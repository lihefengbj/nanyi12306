"""车站代码表：拉取、解析、缓存 station_name.js。

数据格式: @bji|北京|BJP|beijing|bj|0|... 每段以 @ 分隔，字段以 | 分隔。
字段索引: 1=站名, 2=三字码, 3=全拼, 4=简拼
"""
import json
import time
from dataclasses import dataclass

from .paths import data_file
from .session import BASE_URL, Session

STATION_NAME_URL = f"{BASE_URL}/otn/resources/js/framework/station_name.js"
CACHE_TTL = 7 * 24 * 3600  # 7 天


@dataclass(frozen=True)
class Station:
    name: str       # 北京南
    code: str       # VNP
    pinyin: str     # beijingnan
    short: str      # bjn


class StationTable:
    def __init__(self, session: Session) -> None:
        self._session = session
        self._by_name: dict[str, Station] = {}
        self._by_code: dict[str, Station] = {}

    def load(self, force: bool = False) -> None:
        cache = data_file("stations.json")
        if not force and cache.exists():
            try:
                raw = json.loads(cache.read_text(encoding="utf-8"))
                if time.time() - raw.get("fetched_at", 0) < CACHE_TTL:
                    self._fill(raw["stations"])
                    return
            except (OSError, json.JSONDecodeError, KeyError):
                pass
        self._fetch_and_cache(cache)

    def _fetch_and_cache(self, cache) -> None:
        resp = self._session.get(STATION_NAME_URL)
        text = resp.text
        stations: dict[str, dict] = {}
        for record in text.split("@")[1:]:
            parts = record.split("|")
            if len(parts) < 5:
                continue
            name, code, pinyin, short = parts[1], parts[2], parts[3], parts[4]
            stations[name] = {"code": code, "pinyin": pinyin, "short": short}
        cache.write_text(
            json.dumps({"fetched_at": time.time(), "stations": stations},
                       ensure_ascii=False),
            encoding="utf-8",
        )
        self._fill(stations)

    def _fill(self, raw: dict[str, dict]) -> None:
        self._by_name.clear()
        self._by_code.clear()
        for name, info in raw.items():
            st = Station(name, info["code"], info.get("pinyin", ""), info.get("short", ""))
            self._by_name[name] = st
            self._by_code[st.code] = st

    # ---------- 查询 ----------

    def by_name(self, name: str) -> Station | None:
        return self._by_name.get(name)

    def by_code(self, code: str) -> Station | None:
        return self._by_code.get(code)

    def search(self, keyword: str, limit: int = 12) -> list[Station]:
        """模糊搜索：站名/全拼/简拼前缀或包含匹配，供 UI 自动补全。"""
        kw = keyword.strip().lower()
        if not kw:
            return []
        hits: list[Station] = []
        for st in self._by_name.values():
            if (kw in st.name
                    or st.pinyin.startswith(kw)
                    or st.short.startswith(kw)):
                hits.append(st)
            if len(hits) >= limit:
                break
        return hits

    def __len__(self) -> int:
        return len(self._by_name)
