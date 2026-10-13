"""下单链路：正常购票 + 候补兜底。"""
import json
import logging
import re
import time
import traceback
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from .auth import Passenger
from .js_literal import LiteralParseError, LiteralReader
from .query import TrainTicket, seat_type_code
from .session import BASE_URL, Session, _response_kind, _safe_url

Log = Callable[[str], None]

logger = logging.getLogger("ny12306.order")


class OrderError(Exception):
    pass


class AuthExpiredError(OrderError):
    pass


class NoTicketError(OrderError):
    """票在提交前已被抢走，调用方应回到轮询循环。"""


class ManualActionError(OrderError):
    """Require the official app before any further automatic submission."""


class QueueResult(Enum):
    SUCCESS = "success"
    FAILED = "failed"
    PENDING = "pending"


@dataclass
class OrderOutcome:
    result: QueueResult
    order_id: str = ""
    message: str = ""


def _safe_rejection_reason(data: dict[str, Any]) -> str:
    """Report only fixed, recognized reasons; never echo server-supplied text."""
    raw = data.get("messages") or data.get("message") or []
    messages = [raw] if isinstance(raw, str) else raw if isinstance(raw, list) else []
    payload = data.get("data")
    if isinstance(payload, dict) and isinstance(payload.get("errMsg"), str):
        messages = [*messages, payload["errMsg"]]
    text = " ".join(message[:2000] for message in messages[:8] if isinstance(message, str))
    reasons = (
        (("未登录", "重新登录", "登录失效"), "需要重新登录"),
        (("没有可用的票", "余票不足", "车票已售完", "无票"), "余票不足"),
        (("排队人数", "排队过多", "排队失败"), "排队人数超限，稍后重试"),
        (("距开车时间", "开车时间", "停止售票", "已停售", "停售", "预售期"), "距发车太近或已停止售票"),
        (("限购", "购票次数"), "超出购票限制，请在 12306 App 查看"),
        (("行程冲突", "行程有冲突"), "与已有订单行程冲突，请在 12306 App 查看"),
        (("身份信息", "证件信息", "身份核验", "核验"), "乘车人身份信息核验未通过，请在 12306 App 处理"),
        (("常用联系人", "联系人"), "常用联系人数量或状态异常，请在 12306 App 处理"),
        (("未完成订单", "未支付订单"), "存在未完成订单，请在 12306 App 处理"),
        (("请勿重复提交", "重复提交"), "订单可能已存在，请在 12306 App 查看"),
        (("验证码", "实名核验"), "需要官方验证"),
        (("日期格式", "日期错误", "日期不合法"), "乘车日期无效"),
        (("参数错误", "参数不完整", "参数不合法"), "请求参数校验失败"),
        (("提交失败，请重试", "提交失败,请重试"), "提交失败，请重试（服务端未说明具体原因）"),
        (("非法的订票请求",), "请求被判定为非法订票请求（协议字段问题），请反馈日志"),
        (("系统忙", "系统繁忙"), "系统繁忙"),
    )
    for phrases, reason in reasons:
        if any(phrase in text for phrase in phrases):
            return reason
    return f"服务端拒绝请求，原因未识别（消息长度={len(text)}，正文未记录）"


def _require_login(data: dict[str, Any]) -> None:
    if not isinstance(data, dict):
        raise OrderError("订单响应结构无效")
    if data.get("status") is not True:
        msg = str(data.get("messages") or data.get("message") or "")
        if "未登录" in msg or "重新登录" in msg or "登录失效" in msg:
            raise AuthExpiredError("订单接口要求重新登录")
        if any(word in msg for word in ("没有可用的票", "余票不足", "车票已售完", "无票")):
            raise NoTicketError("服务端明确返回余票不足")
        raise OrderError(_safe_rejection_reason(data))


def _checked_response(data: dict, step: str, submit_status: bool = False) -> dict:
    payload = data.get("data") if isinstance(data, dict) else None

    def boolean_status(value):
        return value if isinstance(value, bool) else "(missing/invalid)"

    logger.debug("订单步骤=%s status=%s submit_status=%s data_fields=%s",
              step, boolean_status(data.get("status")) if isinstance(data, dict) else None,
              boolean_status(payload.get("submitStatus")) if isinstance(payload, dict) else None,
              sorted(payload) if isinstance(payload, dict) else [])
    if isinstance(data, dict) and (
            data.get("status") is not True
            or (submit_status and isinstance(payload, dict)
                and payload.get("submitStatus") is not True)):
        logger.warning("订单拒绝步骤=%s reason=%s", step, _safe_rejection_reason(data))
        # Local-file-only diagnostics for unrecognized rejections. 12306 errMsg
        # values are fixed business templates; they are still excluded from the
        # UI but recorded here to make one-off causes diagnosable.
        if isinstance(payload, dict):
            err_msg = str(payload.get("errMsg") or "").strip()
            if err_msg and "原因未识别" in _safe_rejection_reason(data):
                logger.debug("订单拒绝原文步骤=%s errMsg=%r", step, err_msg[:200])
    try:
        _require_login(data)
    except OrderError as exc:
        raise type(exc)(f"{step}: {exc}") from exc
    if submit_status and (
            not isinstance(payload, dict) or payload.get("submitStatus") is not True):
        message = str(payload.get("errMsg") or "") if isinstance(payload, dict) else ""
        if any(word in message for word in ("没有可用的票", "余票不足", "无票")):
            raise NoTicketError(f"{step}: 余票不足")
        raise OrderError(f"{step}: {_safe_rejection_reason(data)}")
    return payload if isinstance(payload, dict) else {}


def _js_object(html: str, name: str) -> dict:
    """Parse a server-embedded data object without executing JavaScript."""
    match = re.search(rf"\b{re.escape(name)}\s*=\s*", html)
    if not match:
        raise OrderError(f"initDc 缺少 {name}")
    try:
        return LiteralReader(html, match.end()).read_object()
    except LiteralParseError as exc:
        logger.warning("initDc 对象解析失败 object=%s reason=%s offset=%d",
                       name, exc.reason, exc.offset)
        # No source snippet, exception message from third-party libraries, or
        # field value is included in either the log or the UI.
        raise OrderError(
            f"initDc 的 {name} 数据解析失败"
            f"（语法={exc.reason}，偏移={exc.offset}），停止提交"
        ) from None


@dataclass
class OrderContext:
    token: str
    key_check: str
    left_ticket: str
    train_location: str
    form: dict
    request: dict


def _update_form_context(response: dict, context: dict[str, str]) -> None:
    """Carry the same opaque form fields as the site's common_js wrapper."""
    for source, target in (("attributes", "_json_att"),
                           ("repeatSubmitToken", "REPEAT_SUBMIT_TOKEN")):
        value = response.get(source)
        if value:
            if not isinstance(value, str):
                raise OrderError(f"订单表单上下文 {source} 类型无效")
            context[target] = value
    logger.debug("订单表单上下文 attributes_present=%s repeat_token_present=%s",
                 bool(context.get("_json_att")), bool(context.get("REPEAT_SUBMIT_TOKEN")))


def _order_context(session: Session, ticket: TrainTicket,
                   form_context: dict[str, str]) -> OrderContext:
    # Match bp()/otsRedirect(): a normal same-origin HTML form navigation,
    # including its route flag and opaque fields from preceding JSON responses.
    special_stations = {"VCM", "VOM", "VBM", "VFM", "VJM", "YTM"}
    flag = "Y" if {ticket.from_code, ticket.to_code} & special_stations else "N"
    resp = session.post(
        f"{BASE_URL}/otn/confirmPassenger/initDc?{flag}",
        data=dict(form_context),
        headers={
            "Referer": f"{BASE_URL}/otn/leftTicket/init",
            "Origin": BASE_URL,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "X-Requested-With": None,
        },
        retries=1,
    )
    final = urlsplit(resp.url)
    logger.debug("initDc 页面导航 flag=%s http_status=%s final_url=%s kind=%s",
                 flag, resp.status_code, _safe_url(resp.url), _response_kind(resp))
    if (resp.status_code != 200 or final.hostname != urlsplit(BASE_URL).hostname
            or final.path != "/otn/confirmPassenger/initDc"):
        raise OrderError(
            f"initDc 未返回本站订单页（HTTP {resp.status_code}，"
            f"最终 URL={_safe_url(resp.url)}，响应类型={_response_kind(resp)}），停止提交")
    html = resp.text
    m = re.search(r"""globalRepeatSubmitToken\s*=\s*['"]([^'"]+)['"]""", html)
    form = _js_object(html, "ticketInfoForPassengerForm")
    request = _js_object(html, "orderRequestDTO")
    values = [form.get(k) for k in ("key_check_isChange", "leftTicketStr", "train_location")]
    logger.debug("initDc 上下文 token_present=%s key_present=%s left_ticket_present=%s "
              "location_present=%s", bool(m), *[bool(v) for v in values])
    if not m or not all(isinstance(v, str) and v for v in values):
        raise OrderError("initDc 订单上下文缺少必要字段，停止提交")
    return OrderContext(m.group(1), *values, form, request)


def _build_passenger_strs(seat_code: str,
                          passengers: list[Passenger]) -> tuple[str, str]:
    tickets, olds = [], []
    for p in passengers:
        tickets.append(
            f"{seat_code},0,{p.passenger_type},{p.name},{p.id_type},"
            f"{p.id_no},{p.mobile},N,{p.code}"
        )
        olds.append(f"{p.name},{p.id_type},{p.id_no},{p.passenger_type}_")
    return "_".join(tickets), "".join(olds)


def _wait_queue(session: Session, log: Log, timeout: float = 300.0,
                token: str = "", form_context: dict[str, str] | None = None) -> OrderOutcome:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            data = session.get_json(
                f"{BASE_URL}/otn/confirmPassenger/queryOrderWaitTime",
                params={"random": str(int(time.time() * 1000)), "tourFlag": "dc",
                        "_json_att": "", "REPEAT_SUBMIT_TOKEN": token,
                        **(form_context or {})},
                headers={"Referer": f"{BASE_URL}/otn/confirmPassenger/initDc"},
            )
        except Exception as exc:
            log(f"查询排队状态异常（{type(exc).__name__}），继续查询，不重复提交订单")
            time.sleep(2)
            continue
        try:
            d = _checked_response(data, "queryOrderWaitTime")
        except OrderError as exc:
            raise ManualActionError(
                "queryOrderWaitTime: 订单状态无法确认，请检查 12306 App，停止重提"
            ) from exc
        if d.get("orderId"):
            session.order_pending = True
            return OrderOutcome(QueueResult.SUCCESS, order_id=d["orderId"])
        if d.get("msg") and not d.get("queryOrderWaitTimeStatus"):
            raise ManualActionError("排队状态异常，请到 12306 App 检查未完成订单")
        wait = d.get("waitTime")
        count = d.get("waitCount")
        if str(wait) in ("-2", "-3"):
            session.order_pending = False
            return OrderOutcome(QueueResult.FAILED, message="服务端明确结束排队，未生成订单")
        if str(wait) in ("-1", "-100"):
            raise ManualActionError("排队已结束但未返回订单号，请检查 12306 App")
        if wait is not None and str(wait) not in ("-1", ""):
            log(f"排队中：前方约 {count} 人，预计 {wait} 秒")
        time.sleep(2)
    raise ManualActionError("排队查询超时，订单可能已受理；请检查 12306 App，停止重复提交")


def submit_order(session: Session, ticket: TrainTicket, seat_name: str,
                 passengers: list[Passenger], log: Log) -> OrderOutcome:
    """正常下单全流程。无票抛 NoTicketError，登录失效抛 AuthExpiredError。"""
    with session.transaction():
        if session.order_pending:
            raise ManualActionError("本账号已有已受理或状态待确认的订单，请先在 12306 App 处理")
        if time.monotonic() < session._order_not_before:
            raise OrderError("本账号处于下单失败冷却期，本轮不提交")
        try:
            result = _submit_order(session, ticket, seat_name, passengers, log)
        except ManualActionError:
            session.order_pending = True
            raise
        except Exception as exc:
            session._order_failures += 1
            delay = min(5.0 * 2 ** min(session._order_failures - 1, 3), 30.0)
            session._order_not_before = time.monotonic() + delay
            if isinstance(exc, OrderError):
                raise
            # Record code locations, not exception text/locals or request data.
            locations = [
                f"{Path(frame.f_code.co_filename).name}:{line}:{frame.f_code.co_name}"
                for frame, line in traceback.walk_tb(exc.__traceback__)
            ]
            logger.error("下单内部异常 type=%s locations=%s",
                         type(exc).__name__, " -> ".join(locations))
            raise OrderError(f"下单流程异常（{type(exc).__name__}），参见安全 HTTP 日志") from exc
        session._order_failures = 0
        session._order_not_before = time.monotonic() + 5.0
        return result


def _submit_order(session: Session, ticket: TrainTicket, seat_name: str,
                  passengers: list[Passenger], log: Log) -> OrderOutcome:
    if not passengers:
        raise OrderError("未选择乘车人")
    try:
        valid_date = date.fromisoformat(ticket.date).isoformat() == ticket.date
    except ValueError:
        valid_date = False
    if not valid_date:
        raise OrderError("乘车日期必须为 YYYY-MM-DD，不能使用列车始发日")
    seat_code = seat_type_code(seat_name)
    if seat_name == "无座":
        # The official UI maps no-seat to the underlying coach type.
        for seat in ("硬座", "二等座", "硬卧", "一等座", "软座",
                     "软卧", "特等座", "高级软卧", "商务座"):
            if ticket.seats.get(seat) not in (None, "", "--"):
                seat_code = seat_type_code(seat)
                break
        else:
            raise OrderError("无法确定无座对应的车厢席别，停止提交")
    log(f"提交订单 {ticket.code} {seat_name} x{len(passengers)}")

    login = _order_post(session, "checkUser",
        f"{BASE_URL}/otn/login/checkUser", data={"_json_att": ""},
        headers={"Referer": f"{BASE_URL}/otn/leftTicket/init"}, retries=1)
    payload = _checked_response(login, "checkUser")
    if payload.get("flag") is not True:
        raise AuthExpiredError("checkUser: 登录状态已失效")
    form_context = {"_json_att": ""}
    _update_form_context(login, form_context)
    logger.debug("submitOrderRequest 乘车日期=%s 列车始发日=%s 席别=%s",
              ticket.date, ticket.start_train_date or "(missing)", seat_code)
    data = _order_post(session, "submitOrderRequest",
        f"{BASE_URL}/otn/leftTicket/submitOrderRequest",
        data={
            "secretStr": ticket.secret,
            "train_date": ticket.date,
            "back_train_date": ticket.date,
            "tour_flag": "dc",
            "purpose_codes": "ADULT",
            "query_from_station_name": ticket.from_station,
            "query_to_station_name": ticket.to_station,
            "bed_level_info": ticket.bed_level_info,
            "seat_discount_info": ticket.seat_discount_info,
            **form_context,
        },
        headers={"Referer": f"{BASE_URL}/otn/leftTicket/init", "Origin": BASE_URL},
        retries=1,
    )
    _checked_response(data, "submitOrderRequest")
    _update_form_context(data, form_context)

    context = _order_context(session, ticket, form_context)
    if context.request.get("train_no") != ticket.train_no:
        raise OrderError("initDc 车次与本轮所选车次不一致，停止提交")
    raw_date = context.request.get("train_date")
    if (not isinstance(raw_date, dict)
            or not isinstance(raw_date.get("time"), (int, float))):
        raise OrderError("initDc 缺少订单乘车日期")
    dt = datetime.fromtimestamp(raw_date["time"] / 1000,
                                timezone(timedelta(hours=8)))
    if dt.date().isoformat() != ticket.date:
        raise OrderError("initDc 订单乘车日期与所选日期不一致，停止提交")
    token, key_check = context.token, context.key_check
    form_context["REPEAT_SUBMIT_TOKEN"] = token
    referer = f"{BASE_URL}/otn/confirmPassenger/initDc"
    dto = _order_post(session, "getPassengerDTOs",
        f"{BASE_URL}/otn/confirmPassenger/getPassengerDTOs",
        data=dict(form_context),
        headers={"Referer": referer}, retries=1)
    available = _checked_response(dto, "getPassengerDTOs").get("normal_passengers")
    _update_form_context(dto, form_context)
    if not isinstance(available, list):
        raise OrderError("getPassengerDTOs: 乘车人数据结构无效")
    selected = []
    for passenger in passengers:
        match = next((p for p in available if isinstance(p, dict)
                      and p.get("passenger_id_type_code") == passenger.id_type
                      and p.get("passenger_id_no") == passenger.id_no), None)
        if not match or not match.get("allEncStr"):
            raise OrderError("getPassengerDTOs: 所选乘车人缺少本轮订单加密标识")
        selected.append(Passenger(
            passenger.name, passenger.id_type, passenger.id_no, passenger.mobile,
            passenger.passenger_type, match["allEncStr"]))
    p_str, old_str = _build_passenger_strs(seat_code, selected)

    data = _order_post(session, "checkOrderInfo",
        f"{BASE_URL}/otn/confirmPassenger/checkOrderInfo",
        data={
            "cancel_flag": "2", "bed_level_order_num": "000000000000000000000000000000",
            "passengerTicketStr": p_str, "oldPassengerStr": old_str,
            "tour_flag": "dc", "randCode": "", "whatsSelect": "1",
            **form_context,
        },
        headers={"Referer": referer}, retries=1,
    )
    checked = _checked_response(data, "checkOrderInfo", submit_status=True)
    _update_form_context(data, form_context)
    if checked.get("ifShowPassCode") == "Y":
        raise ManualActionError("checkOrderInfo: 需要验证码，请到官方渠道完成验证")
    if checked.get("get608Msg"):
        raise ManualActionError("checkOrderInfo: 需要实名信息核验，请到官方渠道处理")
    query_dto = context.form.get("queryLeftTicketRequestDTO")
    if not isinstance(query_dto, dict):
        raise OrderError("initDc 缺少余票订单上下文")
    if str(context.form.get("isAsync")) == "1" and query_dto.get("ypInfoDetail"):
        request = context.request
        # Locale-independent equivalent of the site's JavaScript Date.toString().
        day = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")[dt.weekday()]
        month = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
                 "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")[dt.month - 1]
        js_date = f"{day} {month} {dt.day:02d} {dt.year} 00:00:00 GMT+0800 (China Standard Time)"
        queue = _order_post(session, "getQueueCount",
            f"{BASE_URL}/otn/confirmPassenger/getQueueCount",
            data={"train_date": js_date, "train_no": request["train_no"],
                  "stationTrainCode": request["station_train_code"], "seatType": seat_code,
                  "fromStationTelecode": request["from_station_telecode"],
                  "toStationTelecode": request["to_station_telecode"],
                  "leftTicket": query_dto["ypInfoDetail"],
                  "purpose_codes": context.form.get("purpose_codes", "ADULT"),
                  "train_location": context.train_location,
                  "isCheckOrderInfo": checked.get("isCheckOrderInfo", ""),
                  **form_context},
            headers={"Referer": referer}, retries=1)
        queue_data = _checked_response(queue, "getQueueCount")
        _update_form_context(queue, form_context)
        if queue_data.get("isRelogin") == "Y":
            raise AuthExpiredError("getQueueCount: 需要重新登录")
        if str(queue_data.get("op_2")).lower() == "true":
            raise NoTicketError("getQueueCount: 排队人数超过余票数量")

    session.order_pending = True
    try:
        data = session.post_json(
            f"{BASE_URL}/otn/confirmPassenger/confirmSingleForQueue",
            data={
                "passengerTicketStr": p_str, "oldPassengerStr": old_str,
                "randCode": "",
                "purpose_codes": str(context.form.get("purpose_codes") or "ADULT"),
                "key_check_isChange": key_check, "leftTicketStr": context.left_ticket,
                "train_location": context.train_location, "choose_seats": "",
                "seatDetailType": "000", "whatsSelect": "1", "roomType": "00", "dwAll": "N",
                "is_jy": "N", "is_cj": "N",
                "encryptedData": "",
                **form_context,
            },
            headers={"Referer": referer}, retries=1,
        )
    except Exception as exc:
        raise ManualActionError(
            "confirmSingleForQueue: 响应异常，订单可能已受理；请检查 12306 App，停止重提"
        ) from exc
    try:
        _checked_response(data, "confirmSingleForQueue", submit_status=True)
    except OrderError:
        payload = data.get("data") if isinstance(data, dict) else None
        if isinstance(payload, dict) and payload.get("submitStatus") is False:
            session.order_pending = False
            message = str(payload.get("errMsg") or "")
            if "重复提交" in message:
                raise ManualActionError(
                    "confirmSingleForQueue: 服务端提示请勿重复提交，订单可能已生成或已进入排队；"
                    "请先在 12306 App 查看未完成订单，停止自动重提"
                )
        else:
            raise ManualActionError(
                "confirmSingleForQueue: 未明确返回拒绝状态，请检查 12306 App，停止重提"
            )
        raise

    log("订单已提交，进入排队队列")
    return _wait_queue(session, log, token=token, form_context=form_context)


def _order_post(session: Session, step: str, url: str, **kwargs) -> dict:
    try:
        return session.post_json(url, **kwargs)
    except Exception as exc:
        raise OrderError(f"{step}: 请求/响应异常（{type(exc).__name__}），参见安全 HTTP 日志") from exc


def submit_candidate(session: Session, ticket: TrainTicket, seat_name: str,
                     passengers: list[Passenger], log: Log) -> OrderOutcome:
    """Do not send the old, incomplete candidate payload on an order failure."""
    raise ManualActionError("自动候补流程尚未验证，未发送候补请求；请到 12306 App 提交候补")


def pending_orders(session: Session) -> list[dict[str, Any]]:
    """查询未完成订单（辅助功能）。"""
    data = session.post_json(
        f"{BASE_URL}/otn/queryOrder/queryMyOrderNoComplete",
        data={"_json_att": ""},
        headers={"Referer": f"{BASE_URL}/otn/queryOrder/init"},
    )
    if not data.get("status"):
        return []
    return json.loads(data.get("data", {}).get("orderDBList", "[]") or "[]")
