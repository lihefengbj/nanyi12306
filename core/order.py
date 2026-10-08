"""下单链路：正常购票 + 候补兜底。"""
import json
import logging
import re
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable

from .auth import Passenger
from .query import TrainTicket, seat_type_code
from .session import BASE_URL, Session

Log = Callable[[str], None]

log = logging.getLogger("ny12306.order")



class OrderError(Exception):
    pass


class AuthExpiredError(OrderError):
    pass


class NoTicketError(OrderError):
    """票在提交前已被抢走，调用方应回到轮询循环。"""


class QueueResult(Enum):
    SUCCESS = "success"
    FAILED = "failed"
    PENDING = "pending"


@dataclass
class OrderOutcome:
    result: QueueResult
    order_id: str = ""
    message: str = ""


def _require_login(data: dict[str, Any]) -> None:
    if not data.get("status"):
        msg = str(data.get("messages") or data.get("message") or "")
        if "登录" in msg or "用户" in msg or data.get("data") == {}:
            raise AuthExpiredError(msg or "登录状态已失效")
        raise OrderError(msg or "请求失败")


def _token_from_init_dc(session: Session) -> tuple[str, str]:
    resp = session.get(
        f"{BASE_URL}/otn/confirmPassenger/initDc",
        headers={"Referer": f"{BASE_URL}/otn/leftTicket/init"},
    )
    html = resp.text
    m = re.search(r"globalRepeatSubmitToken\s*=\s*'([0-9a-f]+)'", html)
    k = re.search(r"key_check_isChange'\s*:\s*'([0-9A-F]+)'", html)
    if not m or not k:
        raise OrderError("解析 REPEAT_SUBMIT_TOKEN 失败")
    return m.group(1), k.group(1)


def _build_passenger_strs(seat_code: str,
                          passengers: list[Passenger]) -> tuple[str, str]:
    tickets, olds = [], []
    for p in passengers:
        tickets.append(
            f"{seat_code},0,{p.passenger_type},{p.name},{p.id_type},"
            f"{p.id_no},{p.mobile},N,{p.code}".rstrip(",")
        )
        olds.append(f"{p.name},{p.id_type},{p.id_no},{p.passenger_type}_")
    return "_".join(tickets), "".join(olds)


def _wait_queue(session: Session, log: Log, timeout: float = 300.0) -> OrderOutcome:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            data = session.get_json(
                f"{BASE_URL}/otn/confirmPassenger/queryOrderWaitTime",
                params={"random": str(int(time.time() * 1000)), "tourFlag": "dc",
                        "_json_att": ""},
                headers={"Referer": f"{BASE_URL}/otn/confirmPassenger/initDc"},
            )
        except Exception as exc:
            log(f"查询排队状态异常: {exc}")
            time.sleep(2)
            continue
        d = data.get("data", {})
        if d.get("orderId"):
            return OrderOutcome(QueueResult.SUCCESS, order_id=d["orderId"])
        if d.get("msg") and not d.get("queryOrderWaitTimeStatus"):
            return OrderOutcome(QueueResult.FAILED, message=str(d.get("msg")))
        wait = d.get("waitTime")
        count = d.get("waitCount")
        if wait is not None and str(wait) not in ("-1", ""):
            log(f"排队中：前方约 {count} 人，预计 {wait} 秒")
        time.sleep(2)
    return OrderOutcome(QueueResult.FAILED, message="排队超时")


def submit_order(session: Session, ticket: TrainTicket, seat_name: str,
                 passengers: list[Passenger], log: Log) -> OrderOutcome:
    """正常下单全流程。无票抛 NoTicketError，登录失效抛 AuthExpiredError。"""
    seat_code = seat_type_code(seat_name)
    log(f"提交订单 {ticket.code} {seat_name} x{len(passengers)}")

    data = session.post_json(
        f"{BASE_URL}/otn/leftTicket/submitOrderRequest",
        data={
            "secret": ticket.secret,
            "train_date": ticket.date,
            "back_train_date": ticket.date,
            "tour_flag": "dc",
            "purpose_codes": "ADULT",
            "query_from_station_name": ticket.from_station,
            "query_to_station_name": ticket.to_station,
            "undefined": "",
        },
        headers={"Referer": f"{BASE_URL}/otn/leftTicket/init"},
    )
    _require_login(data)
    msgs = str(data.get("messages") or "")
    if "票" in msgs and ("不" in msgs or "无" in msgs or "没有" in msgs):
        raise NoTicketError(msgs)

    token, key_check = _token_from_init_dc(session)
    p_str, old_str = _build_passenger_strs(seat_code, passengers)
    referer = f"{BASE_URL}/otn/confirmPassenger/initDc"

    data = session.post_json(
        f"{BASE_URL}/otn/confirmPassenger/checkOrderInfo",
        data={
            "cancel_flag": "2", "bed_level_order_num": "000000000000000000000000000000",
            "passengerTicketStr": p_str, "oldPassengerStr": old_str,
            "tour_flag": "dc", "randCode": "", "whatsSelect": "1",
            "_json_att": "", "REPEAT_SUBMIT_TOKEN": token,
        },
        headers={"Referer": referer},
    )
    _require_login(data)
    if not data.get("data", {}).get("submitStatus", True):
        raise NoTicketError(str(data.get("data", {}).get("errMsg", "订单校验失败")))

    data = session.post_json(
        f"{BASE_URL}/otn/confirmPassenger/confirmSingleForQueue",
        data={
            "passengerTicketStr": p_str, "oldPassengerStr": old_str,
            "randCode": "", "purpose_codes": "ADULT",
            "key_check_isChange": key_check, "leftTicketStr": ticket.secret,
            "train_location": "", "choose_seats": "", "seatDetailType": "000",
            "whatsSelect": "1", "roomType": "00", "dwAll": "N",
            "_json_att": "", "REPEAT_SUBMIT_TOKEN": token,
        },
        headers={"Referer": referer},
    )
    _require_login(data)
    if not data.get("data", {}).get("submitStatus", True):
        raise NoTicketError(str(data.get("data", {}).get("errMsg", "确认订单失败")))

    log("订单已提交，进入排队队列")
    return _wait_queue(session, log)


def submit_candidate(session: Session, ticket: TrainTicket, seat_name: str,
                     passengers: list[Passenger], log: Log) -> OrderOutcome:
    """候补下单（tour_flag=dc，confirmHB）。仅在该车次支持候补上调用。"""
    seat_code = seat_type_code(seat_name)
    log(f"提交候补 {ticket.code} {seat_name}")
    token, _ = _token_from_init_dc(session)
    p_str, old_str = _build_passenger_strs(seat_code, passengers)
    referer = f"{BASE_URL}/otn/confirmPassenger/initDc"

    data = session.post_json(
        f"{BASE_URL}/otn/confirmPassenger/confirmHB",
        data={
            "passengerTicketStr": p_str, "oldPassengerStr": old_str,
            "randCode": "", "purpose_codes": "ADULT",
            "key_check_isChange": "", "leftTicketStr": ticket.secret,
            "train_location": "", "choose_seats": "", "seatDetailType": "000",
            "is_jy": "N", "is_cj": "Y", "dwAll": "N", "whatsSelect": "1",
            "roomType": "00", "hbInfo": "", "jzdhDate": "", "jzdhHour": "",
            "_json_att": "", "REPEAT_SUBMIT_TOKEN": token,
        },
        headers={"Referer": referer},
    )
    _require_login(data)
    if not data.get("data", {}).get("submitStatus", True):
        return OrderOutcome(QueueResult.FAILED,
                            message=str(data.get("data", {}).get("errMsg", "候补提交失败")))
    log("候补已提交，等待兑现")
    return _wait_queue(session, log)


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
