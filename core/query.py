"""余票查询与 33 字段解析、车次过滤。"""
import logging
from dataclasses import dataclass, field
from typing import Any

from .session import BASE_URL, Session

log = logging.getLogger("ny12306.query")

QUERY_URL = f"{BASE_URL}/otn/leftTicket/query"

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

    def seat_available(self, seat_name: str) -> bool:
        v = self.seats.get(seat_name, "")
        return v == "有" or (v.isdigit() and int(v) > 0)


def _parse_train(raw: str, station_map: dict[str, str]) -> TrainTicket | None:
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
        date=parts[IDX["date"]],
        can_buy=parts[IDX["can_buy"]] == "Y",
        seats=seats,
    )


def query_tickets(session: Session, train_date: str, from_code: str,
                  to_code: str, purpose: str = "ADULT") -> list[TrainTicket]:
    """查询余票。train_date 格式 YYYY-MM-DD。"""
    if not session._http.cookies.get("JSESSIONID"):
        # 12306 要求先访问 init 页建立会话 Cookie，否则查询接口返回空
        session.get(f"{BASE_URL}/otn/leftTicket/init")
    data = session.get_json(QUERY_URL, params={
        "leftTicketDTO.train_date": train_date,
        "leftTicketDTO.from_station": from_code,
        "leftTicketDTO.to_station": to_code,
        "purpose_codes": purpose,
    }, headers={"Referer": f"{BASE_URL}/otn/leftTicket/init"})
    if not data.get("status"):
        raise RuntimeError(f"余票查询失败: {data.get('messages') or data.get('message')}")
    result = data.get("data", {}).get("result", [])
    station_map = data.get("data", {}).get("map", {})
    log.debug("查询 %s %s->%s: %d 条", train_date, from_code, to_code, len(result))
    trains = []
    for raw in result:
        t = _parse_train(raw, station_map)
        if t:
            trains.append(t)
    return trains


@dataclass
class TicketFilter:
    train_codes: list[str] = field(default_factory=list)   # 车次白名单，空=全部
    seat_priority: list[str] = field(default_factory=list)  # 席别优先级
    time_from: str = ""    # "06:00"
    time_to: str = ""      # "12:00"
    only_available: bool = True

    def match(self, t: TrainTicket) -> bool:
        if self.train_codes and t.code not in self.train_codes:
            return False
        if self.time_from and t.start_time < self.time_from:
            return False
        if self.time_to and t.start_time > self.time_to:
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
