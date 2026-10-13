"""扫码登录：二维码获取、状态轮询、换取业务会话、乘车人拉取。"""
import base64
import logging
import time
from dataclasses import dataclass
from enum import Enum
from urllib.parse import urlsplit

from requests.cookies import create_cookie

from .session import BASE_URL, Session, _response_kind, _safe_url

log = logging.getLogger("ny12306.auth")

LOGIN_PAGE = f"{BASE_URL}/otn/resources/login.html"
PAGE_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "X-Requested-With": None,
}


class QrStatus(Enum):
    WAITING = "等待扫码"
    SCANNED = "已扫码，请在 12306 App 上确认"
    CONFIRMED = "已确认"
    EXPIRED = "二维码已过期"


@dataclass
class Passenger:
    name: str
    id_type: str       # 证件类型代码，如 "1"=二代身份证
    id_no: str
    mobile: str
    passenger_type: str  # 1=成人 2=儿童 3=学生
    code: str = ""     # passenger_uuid


class Auth:
    def __init__(self, session: Session) -> None:
        self._session = session
        self._login_config: dict | None = None
        self._qr_generation = 0
        self._qr_started: float | None = None
        self._last_qr_uuid: str | None = None
        self._last_qr_image: bytes | None = None

    # ---------- 扫码登录 ----------

    def _ensure_qr_login_enabled(self) -> None:
        if self._login_config is None:
            # A new scan must not inherit tickets from a failed/expired scan.
            jar = self._session._http.cookies
            for cookie in list(jar):
                if not cookie.domain or cookie.name in {"uamtk", "tk"}:
                    jar.clear(cookie.domain, cookie.path, cookie.name)
            self._session.get(LOGIN_PAGE, headers=PAGE_HEADERS.copy())
            response = self._session.post_json(
                f"{BASE_URL}/otn/login/conf",
                headers={"Referer": f"{BASE_URL}/otn/resources/login.html"},
            )
            config = response.get("data")
            if not isinstance(config, dict):
                raise RuntimeError("读取 12306 登录配置失败")
            self._login_config = config
            log.debug(
                "12306 登录配置: is_uam_login=%s is_sweep_login=%s",
                str(config.get("is_uam_login", "N")).upper() == "Y",
                str(config.get("is_sweep_login", "N")).upper() == "Y",
            )

        if (str(self._login_config.get("is_uam_login", "N")).upper() != "Y"
                or str(self._login_config.get("is_sweep_login", "N")).upper() != "Y"):
            raise RuntimeError("12306 当前登录配置未启用统一认证扫码登录")

    def create_qr(self) -> tuple[str, bytes]:
        """返回 (uuid, 二维码 PNG 字节)。"""
        self._ensure_qr_login_enabled()
        # Clear tickets before each new QR, not after checkqr has set the
        # server-issued, correctly scoped Cookie.
        jar = self._session._http.cookies
        for cookie in list(jar):
            if cookie.name in {"uamtk", "tk"}:
                jar.clear(cookie.domain, cookie.path, cookie.name)
        data = self._session.post_json(
            f"{BASE_URL}/passport/web/create-qr64",
            data={"appid": "otn"},
            headers={"Referer": f"{BASE_URL}/otn/resources/login.html"},
        )
        if str(data.get("result_code")) != "0":
            raise RuntimeError(f"获取二维码失败: {data.get('result_message')}")
        uuid = data.get("uuid")
        if not isinstance(uuid, str) or not uuid:
            raise RuntimeError("二维码接口未返回有效的扫码标识")
        image = base64.b64decode(data["image"])
        self._qr_generation += 1
        self._qr_started = time.monotonic()
        log.info(
            "二维码创建成功 generation=%d image_bytes=%d "
            "new_uuid=%s new_image=%s",
            self._qr_generation, len(image),
            uuid != self._last_qr_uuid, image != self._last_qr_image,
        )
        self._last_qr_uuid, self._last_qr_image = uuid, image
        return uuid, image

    def check_qr(self, uuid: str) -> tuple[QrStatus, str]:
        """轮询二维码状态。返回 (状态, uamtk|'')。

        result_code: 0=未识别 1=已识别待授权 2=登录成功 3=已失效 5=系统异常
        """
        cookies = self._session._http.cookies
        data = self._session.post_json(
            f"{BASE_URL}/passport/web/checkqr",
            data={
                "RAIL_DEVICEID": cookies.get("RAIL_DEVICEID", ""),
                "RAIL_EXPIRATION": cookies.get("RAIL_EXPIRATION", ""),
                "uuid": uuid, "appid": "otn", "_json_att": "",
            },
            headers={"Referer": f"{BASE_URL}/otn/resources/login.html"},
        )
        code = str(data.get("result_code", ""))
        # Log safe protocol status, never UUID, image, ticket or response body.
        safe_code = code if code.isdigit() and len(code) <= 3 else "(invalid)"
        elapsed = (time.monotonic() - self._qr_started
                   if self._qr_started is not None else 0.0)
        log.debug(
            "checkqr 返回 generation=%d elapsed_seconds=%.1f result_code=%s "
            "ticket_present=%s",
            self._qr_generation, elapsed, safe_code, bool(data.get("uamtk")),
        )
        if code == "2":
            log.debug("二维码状态: 已确认")
            return QrStatus.CONFIRMED, data.get("uamtk", "")
        if code == "1":
            log.debug("二维码状态: 已扫码待确认")
            return QrStatus.SCANNED, ""
        if code == "3":
            log.debug("二维码状态: 已过期")
            return QrStatus.EXPIRED, ""
        if code == "5":
            raise RuntimeError(data.get("result_message") or "二维码状态查询异常")
        if code == "0":
            return QrStatus.WAITING, ""
        raise RuntimeError(
            f"二维码状态接口返回异常 (result_code={code or '缺失'})"
            + (f": {data['result_message']}" if data.get("result_message") else "")
        )

    def finish_login(self, uamtk: str) -> None:
        """按官网登录页的统一认证流程交换业务 Cookie。"""
        if not uamtk:
            raise RuntimeError("扫码登录未返回 uamtk")
        self._ensure_qr_login_enabled()

        # Keep the server's /passport ticket; replacing it with a root cookie
        # changes its scope and needlessly sends it to the business endpoints.
        jar = self._session._http.cookies
        ticket_cookies = [
            c for c in jar if c.name == "uamtk"
            and c.domain.lstrip(".") == "kyfw.12306.cn"
            and c.path == "/passport" and c.value == uamtk
        ]
        keep = ticket_cookies[0] if ticket_cookies else None
        for cookie in list(jar):
            if cookie.name == "uamtk" and cookie is not keep:
                jar.clear(cookie.domain, cookie.path, cookie.name)
        if keep is None:
            ticket = create_cookie(
                "uamtk", uamtk, domain="kyfw.12306.cn", path="/passport",
                secure=True,
            )
            ticket.domain_specified = False
            jar.set_cookie(ticket)
        log.debug("扫码票据已就绪，scope=kyfw.12306.cn/passport")

        # The official login page is initialized as popup_initLogin(false).
        # userLogin redirects to /otn/passport, whose JS exchanges the passport
        # ticket via JSON POST before returning to userLogin.
        landing = self._session.get(
            f"{BASE_URL}/otn/login/userLogin",
            headers={**PAGE_HEADERS, "Referer": LOGIN_PAGE},
        )
        parsed = urlsplit(landing.url)
        if (parsed.scheme != "https" or parsed.hostname != "kyfw.12306.cn"
                or landing.status_code != 200):
            raise RuntimeError(
                f"登录导航异常 (HTTP {landing.status_code}, "
                f"final_url={_safe_url(landing.url)})"
            )
        if parsed.path != "/otn/passport":
            if self.check_user():
                self._session.save_cookies()
                return
            raise RuntimeError("登录导航未进入统一认证中转页，且会话未建立")
        if _response_kind(landing) not in {"html", "xhtml"}:
            raise RuntimeError("统一认证中转页返回类型异常")
        data = self._session.post_json(
            f"{BASE_URL}/passport/web/auth/uamtk",
            data={"appid": "otn"},
            headers={"Referer": landing.url, "Origin": BASE_URL},
        )
        code = str(data.get("result_code", "缺失"))
        log.debug(
            "auth/uamtk 返回 result_code=%s has_business_ticket=%s",
            code, bool(data.get("newapptk") or data.get("apptk")),
        )
        if str(data.get("result_code")) != "0":
            raise RuntimeError(
                f"统一认证票据交换失败 (result_code={data.get('result_code', '缺失')})"
            )
        apptk = data.get("newapptk") or data.get("apptk")
        if not apptk:
            raise RuntimeError("统一认证未返回业务票据")

        result = self._session.post_json(
            f"{BASE_URL}/otn/uamauthclient",
            data={"tk": apptk},
            headers={"Referer": landing.url, "Origin": BASE_URL},
        )
        log.debug("uamauthclient 返回 result_code=%s",
                  str(result.get("result_code", "缺失")))
        if str(result.get("result_code")) != "0":
            raise RuntimeError(
                f"建立 12306 业务会话失败 "
                f"(result_code={result.get('result_code', '缺失')})"
            )
        self._session.get(
            f"{BASE_URL}/otn/login/userLogin",
            headers={**PAGE_HEADERS, "Referer": landing.url},
        )
        if not self.check_user():
            raise RuntimeError(
                "业务票据交换后会话校验未通过，请查看 auth/uamtk 与 checkUser 日志"
            )
        self._session.save_cookies()

    # ---------- 登录态校验 ----------

    def check_user(self) -> bool:
        try:
            data = self._session.post_json(
                f"{BASE_URL}/otn/login/checkUser",
                data={"_json_att": ""},
                headers={"Referer": f"{BASE_URL}/otn/leftTicket/init"},
            )
            payload = data.get("data") or {}
            flag = (data.get("status") is True and isinstance(payload, dict)
                    and payload.get("flag") is True)
            log.debug(
                "checkUser 返回 status=%s login_flag=%s data_fields=%s",
                bool(data.get("status")), flag,
                sorted(payload.keys()) if isinstance(payload, dict) else [],
            )
            return flag
        except Exception as exc:
            log.warning("checkUser 请求或解析失败: %s", type(exc).__name__)
            return False

    def get_user_name(self) -> str:
        try:
            data = self._session.get_json(
                f"{BASE_URL}/otn/index/initMy12306Api",
                headers={"Referer": f"{BASE_URL}/otn/index/init"},
            )
            return data.get("data", {}).get("user_name", "")
        except Exception:
            return ""

    def logout(self) -> None:
        self._session.clear_cookies()
        self._login_config = None

    # ---------- 乘车人 ----------

    def get_passengers(self, retry: int = 2) -> list[Passenger]:
        if not self.check_user():
            raise RuntimeError("登录会话未建立或已失效，请先完成扫码登录")
        errors = []
        # Contact management does not require a submitted train order.
        try:
            return self._passengers_from_query()
        except Exception as exc:
            errors.append(f"passengers/query: {exc}")
        try:
            return self._passengers_from_confirm(retry)
        except Exception as exc:
            errors.append(f"getPassengerDTOs: {exc}")
        raise RuntimeError("；".join(errors) or "获取乘车人列表失败")

    def _passengers_from_confirm(self, retry: int) -> list[Passenger]:
        # getPassengerDTOs 依赖下单上下文，先访问 initDc 建立会话状态
        self._session.get(
            f"{BASE_URL}/otn/confirmPassenger/initDc",
            headers={"Referer": f"{BASE_URL}/otn/leftTicket/init"},
        )
        for _ in range(retry):
            data = self._session.post_json(
                f"{BASE_URL}/otn/confirmPassenger/getPassengerDTOs",
                data={"_json_att": ""},
                headers={"Referer": f"{BASE_URL}/otn/confirmPassenger/initDc"},
            )
            if data.get("status"):
                break
            time.sleep(1)
        else:
            raise RuntimeError("接口返回失败")
        payload = data.get("data") or {}
        raw_list = (payload.get("normal_passengers")
                    or payload.get("normalPassengers") or [])
        log.debug(
            "getPassengerDTOs 返回 status=%s data_fields=%s passenger_count=%d",
            data.get("status"), sorted(payload.keys()), len(raw_list),
        )
        result = []
        for p in raw_list:
            result.append(Passenger(
                name=p.get("passenger_name", ""),
                id_type=p.get("passenger_id_type_code", "1"),
                id_no=p.get("passenger_id_no", ""),
                mobile=p.get("mobile_no", ""),
                passenger_type=p.get("passenger_type", "1"),
                code=p.get("passenger_uuid", ""),
            ))
        if not result:
            raise RuntimeError(
                f"返回为空（账号无常用乘车人或接口结构变化），data 键: {list(payload.keys())}")
        return result

    def _passengers_from_query(self) -> list[Passenger]:
        """备用接口：常用联系人管理页的查询接口。"""
        data = self._session.post_json(
            f"{BASE_URL}/otn/passengers/query",
            data={"pageIndex": "1", "pageSize": "50", "_json_att": ""},
            headers={"Referer": f"{BASE_URL}/otn/passengers/init"},
        )
        if not data.get("status"):
            raise RuntimeError("接口返回失败")
        payload = data.get("data") or {}
        datas = payload.get("datas") or payload.get("normal_passengers") or []
        log.debug(
            "passengers/query 返回 status=%s data_fields=%s passenger_count=%d",
            data.get("status"), sorted(payload.keys()), len(datas),
        )
        result = []
        for p in datas:
            result.append(Passenger(
                name=p.get("passenger_name", ""),
                id_type=p.get("passenger_id_type_code", "1"),
                id_no=p.get("passenger_id_no", ""),
                mobile=p.get("mobile_no", ""),
                passenger_type=p.get("passenger_type", "1"),
                code=p.get("passenger_uuid", ""),
            ))
        if not result:
            raise RuntimeError("返回为空")
        return result
