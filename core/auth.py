"""扫码登录：二维码获取、状态轮询、换取业务会话、乘车人拉取。"""
import base64
import logging
import time
from dataclasses import dataclass
from enum import Enum

from .session import BASE_URL, PASSPORT_URL, Session

log = logging.getLogger("ny12306.auth")


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

    # ---------- 扫码登录 ----------

    def create_qr(self) -> tuple[str, bytes]:
        """返回 (uuid, 二维码 PNG 字节)。"""
        data = self._session.post_json(
            f"{BASE_URL}/passport/web/create-qr64",
            data={"appid": "otn"},
            headers={"Referer": f"{BASE_URL}/otn/resources/login.html"},
        )
        if data.get("result_code") != "0":
            raise RuntimeError(f"获取二维码失败: {data.get('result_message')}")
        return data["uuid"], base64.b64decode(data["image"])

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
        if code == "2":
            return QrStatus.CONFIRMED, data.get("uamtk", "")
        if code == "1":
            return QrStatus.SCANNED, ""
        if code == "3":
            return QrStatus.EXPIRED, ""
        if code == "5":
            raise RuntimeError("二维码状态查询异常")
        return QrStatus.WAITING, ""

    def finish_login(self, uamtk: str) -> None:
        """扫码确认后换取业务 Cookie。"""
        if not uamtk:
            raise RuntimeError("扫码登录未返回 uamtk")
        # checkqr 成功时会 Set-Cookie: uamtk=...; Path=/passport。
        # 若响应头未种（极少数情况），手动补种，但必须与服务器一致：
        # domain 限定 kyfw.12306.cn、Path=/passport，否则重复 Cookie 会导致
        # auth/uamtk 解析错乱返回 302。
        jar = self._session._http.cookies
        uamtk_cookies = [c for c in jar if c.name == "uamtk"]
        if not uamtk_cookies:
            jar.set("uamtk", uamtk, domain="kyfw.12306.cn", path="/passport")
        elif len(uamtk_cookies) > 1:
            keep = uamtk_cookies[0]
            for c in uamtk_cookies[1:]:
                jar.clear(c.domain, c.path, c.name)
            log.debug("清理重复 uamtk Cookie，保留: %s/%s", keep.domain, keep.path)
        log.debug("当前 Cookie: %s", [(c.name, c.path) for c in jar])

        # uamtk -> newapptk（与官网 /otn/passport 中转页 JS 一致）
        data = self._session.post_json(
            f"{BASE_URL}/passport/web/auth/uamtk",
            data={"appid": "otn", "_json_att": ""},
            headers={"Referer": f"{BASE_URL}/otn/passport?redirect=/otn/login/userLogin"},
        )
        log.debug("auth/uamtk 返回: %s", {k: v for k, v in data.items() if k != "newapptk"})
        if str(data.get("result_code")) != "0" or not data.get("newapptk"):
            raise RuntimeError(f"换取 newapptk 失败: {data.get('result_message')}")
        # newapptk -> 业务 Cookie
        data = self._session.post_json(
            f"{BASE_URL}/otn/uamauthclient",
            data={"tk": data["newapptk"]},
            headers={"Referer": f"{BASE_URL}/otn/resources/login.html"},
        )
        log.debug("uamauthclient 返回: %s", {k: v for k, v in data.items() if k != "apptk"})
        if str(data.get("result_code")) != "0":
            raise RuntimeError(f"换取业务票据失败: {data.get('result_message')}")
        self._session.post(
            f"{BASE_URL}/otn/login/userLogin",
            data={"_json_att": ""},
            headers={"Referer": f"{BASE_URL}/otn/resources/login.html"},
        )
        self._session.save_cookies()
        if not self.check_user():
            raise RuntimeError("登录流程完成但会话校验未通过，请重试")

    # ---------- 登录态校验 ----------

    def check_user(self) -> bool:
        try:
            data = self._session.post_json(
                f"{BASE_URL}/otn/login/checkUser",
                data={"_json_att": ""},
                headers={"Referer": f"{BASE_URL}/otn/leftTicket/init"},
            )
            return bool(data.get("data", {}).get("flag"))
        except Exception:
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

    # ---------- 乘车人 ----------

    def get_passengers(self, retry: int = 2) -> list[Passenger]:
        errors = []
        try:
            return self._passengers_from_confirm(retry)
        except Exception as exc:
            errors.append(f"getPassengerDTOs: {exc}")
        try:
            return self._passengers_from_query()
        except Exception as exc:
            errors.append(f"passengers/query: {exc}")
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
        log.debug("getPassengerDTOs 原始返回: %s", str(data)[:800])
        payload = data.get("data") or {}
        raw_list = (payload.get("normal_passengers")
                    or payload.get("normalPassengers") or [])
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
        log.debug("passengers/query 原始返回: %s", str(data)[:800])
        payload = data.get("data") or {}
        datas = payload.get("datas") or payload.get("normal_passengers") or []
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
