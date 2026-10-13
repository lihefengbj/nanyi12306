"""余票查询与 33 字段解析、车次过滤。"""
import logging
import re
from dataclasses import dataclass, field
from datetime import date
from urllib.parse import urlsplit

from .session import BASE_URL, Session

log = logging.getLogger("ny12306.query")

# 席别代码 -> 名称
SEAT_TYPES = {
    "9": "商务座", "P": "特等座", "M": "一等座", "O": "二等座",
    "6": "高级软卧", "4": "软卧", "F": "动卧", "3": "硬卧",
    "2": "软座", "1": "硬座", "WZ": "无座",
}

# 字段索引（以 12306 实际返回为准，如需调整改这里即可）
IDX = {
    "secret": 0, "train_no": 2, "code": 3,
    "start_code": 4, "end_code": 5,
    "from_code": 6, "to_code": 7,
    "start_time": 8, "arrive_time": 9, "duration": 10,
    "can_buy": 11, "date": 13,
    "软卧": 23, "软座": 24, "无座": 26, "硬卧": 28, "硬座": 29,
    "二等座": 30, "一等座": 31, "商务座": 32, "动卧": 33,
    "高级软卧": 21, "特等座": 25,
}


@dataclass
class TrainTicket:
    secret: str          # 下单用加密串（已 URL 解码）
    train_no: str
    code: str            # G103
    from_station: str
    to_station: str
    start_time: str
    arrive_time: str
    duration: str
    date: str
    can_buy: bool
    seats: dict[str, str] = field(default_factory=dict)  # 席别名 -> 余量("有"/数字/"无")
    start_train_date: str = ""  # 列车始发日，不是途经站的乘车日期
    bed_level_info: str = ""
    seat_discount_info: str = ""
    from_code: str = ""
    to_code: str = ""

    def seat_available(self, seat_name: str) -> bool:
        v = self.seats.get(seat_name, "")
        return v == "有" or (v.isdigit() and int(v) > 0)


def _parse_train(raw: str, station_map: dict[str, str],
                 train_date: str) -> TrainTicket | None:
    parts = raw.split("|")
    if len(parts) < 33 or not parts[IDX["secret"]]:
        return None
    from urllib.parse import unquote
    seats: dict[str, str] = {}
    for name, idx in IDX.items():
        if name in SEAT_TYPES.values() and idx < len(parts):
            v = parts[idx]
            seats[name] = "" if v == "--" else v
    return TrainTicket(
        secret=unquote(parts[IDX["secret"]]),
        train_no=parts[IDX["train_no"]],
        code=parts[IDX["code"]],
        from_station=station_map.get(parts[IDX["from_code"]], parts[IDX["from_code"]]),
        to_station=station_map.get(parts[IDX["to_code"]], parts[IDX["to_code"]]),
        start_time=parts[IDX["start_time"]],
        arrive_time=parts[IDX["arrive_time"]],
        duration=parts[IDX["duration"]],
        date=train_date,
        can_buy=parts[IDX["can_buy"]] == "Y",
        seats=seats,
        start_train_date=parts[IDX["date"]],
        bed_level_info=parts[53] if len(parts) > 53 else "",
        seat_discount_info=parts[54] if len(parts) > 54 else "",
        from_code=parts[IDX["from_code"]],
        to_code=parts[IDX["to_code"]],
    )


def _query_endpoint(path: str) -> str:
    """Accept only the same-origin leftTicket query family advertised by 12306."""
    if not isinstance(path, str):
        raise RuntimeError("余票查询入口类型无效")
    parsed = urlsplit(path)
    if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
        raise RuntimeError("拒绝非本站余票查询入口")
    clean = path.removeprefix("/otn/").removeprefix("otn/")
    if not re.fullmatch(r"leftTicket/query[A-Za-z0-9]*", clean):
        raise RuntimeError("余票查询入口路径无效")
    return f"{BASE_URL}/otn/{clean}"


def _init_query(session: Session) -> str:
    response = session.get(
        f"{BASE_URL}/otn/leftTicket/init",
        headers={"Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                 "X-Requested-With": None},
        retries=1,
    )
    final = urlsplit(response.url)
    if (response.status_code != 200
            or final.hostname != urlsplit(BASE_URL).hostname
            or final.path != "/otn/leftTicket/init"):
        raise RuntimeError("余票查询页初始化失败：未返回本站查询页")
    match = re.search(r"\bCLeftTicketUrl\s*=\s*['\"]([^'\"]+)['\"]", response.text)
    if not match:
        raise RuntimeError("余票查询页缺少 CLeftTicketUrl，停止猜测查询接口")
    endpoint = _query_endpoint(match.group(1))
    log.info("余票查询入口: %s", endpoint)
    return endpoint


def query_tickets(session: Session, train_date: str, from_code: str,
                  to_code: str, purpose: str = "ADULT") -> list[TrainTicket]:
    """查询余票。train_date 格式 YYYY-MM-DD。"""
    if date.fromisoformat(train_date).isoformat() != train_date:
        raise ValueError("乘车日期必须为 YYYY-MM-DD")
    params = {
        "leftTicketDTO.train_date": train_date,
        "leftTicketDTO.from_station": from_code,
        "leftTicketDTO.to_station": to_code,
        "purpose_codes": purpose,
    }
    with session.transaction():
        session.wait_for_query()
        try:
            if session._query_url is None:
                session._query_url = _init_query(session)
            for attempt in range(2):
                data = session.get_json(session._query_url, params=params, retries=1,
                    headers={"Referer": f"{BASE_URL}/otn/leftTicket/init",
                             "Cache-Control": "no-cache", "If-Modified-Since": "0"})
                if isinstance(data, dict) and data.get("status") is True:
                    break
                if attempt == 0 and isinstance(data, dict) and data.get("c_url"):
                    session._query_url = _query_endpoint(data["c_url"])
                    log.info("服务端更新余票入口: %s", session._query_url)
                    session.wait_for_query()
                    continue
                raise RuntimeError("余票查询失败：服务端未返回成功状态")
            payload = data.get("data")
            if (not isinstance(payload, dict)
                    or not isinstance(payload.get("result"), list)
                    or not isinstance(payload.get("map", {}), dict)):
                raise RuntimeError("余票查询失败：响应数据结构无效")
            result, station_map = payload["result"], payload.get("map", {})
            trains = []
            for raw in result:
                if not isinstance(raw, str):
                    raise RuntimeError("余票查询失败：车次记录类型无效")
                t = _parse_train(raw, station_map, train_date)
                if t:
                    trains.append(t)
        except Exception:
            session.query_failed()
            raise
        session.query_succeeded()
    log.debug("查询 %s %s->%s: %d 条", train_date, from_code, to_code, len(result))
    return trains


@dataclass
class TicketFilter:
    train_codes: list[str] = field(default_factory=list)   # 车次白名单，空=全部
    seat_priority: list[str] = field(default_factory=list)  # 席别优先级
    time_from: str = ""    # "06:00"
    time_to: str = ""      # "12:00"
    only_available: bool = True

    def match_train(self, t: TrainTicket) -> bool:
        """匹配车次和当日出发时间（包含两个端点），不检查余票。"""
        if self.train_codes and t.code not in self.train_codes:
            return False
        if self.time_from and t.start_time < self.time_from:
            return False
        if self.time_to and t.start_time > self.time_to:
            return False
        return True

    def match(self, t: TrainTicket) -> bool:
        if not self.match_train(t):
            return False
        if self.only_available:
            return self.pick_seat(t) is not None
        return True

    def pick_seat(self, t: TrainTicket) -> str | None:
        """按优先级返回第一个有票的席别名。"""
        for seat in self.seat_priority:
            if t.seat_available(seat):
                return seat
        return None


def apply_filter(trains: list[TrainTicket], flt: TicketFilter) -> list[TrainTicket]:
    return [t for t in trains if flt.match(t)]


def seat_type_code(seat_name: str) -> str:
    for code, name in SEAT_TYPES.items():
        if name == seat_name:
            return code
    raise ValueError(f"未知席别: {seat_name}")
