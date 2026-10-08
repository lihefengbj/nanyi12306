"""12306 HTTP 会话封装：统一请求头、重试、Cookie 持久化。"""
import json
import logging
import threading
import time

import certifi
import requests

from .paths import data_file

log = logging.getLogger("ny12306.http")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

BASE_URL = "https://kyfw.12306.cn"
PASSPORT_URL = "https://passport.12306.cn"


class Session:
    """线程安全的 requests.Session 包装，带 Cookie 持久化与指数退避重试。"""

    def __init__(self) -> None:
        self._http = requests.Session()
        self._http.headers.update({
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "X-Requested-With": "XMLHttpRequest",
        })
        self._lock = threading.Lock()
        self.load_cookies()

    # ---------- Cookie 持久化 ----------

    def load_cookies(self) -> None:
        try:
            raw = json.loads(data_file("cookies.json").read_text(encoding="utf-8"))
            for name, value in raw.items():
                self._http.cookies.set(name, value)
        except (OSError, json.JSONDecodeError):
            pass

    def save_cookies(self) -> None:
        data = {c.name: c.value for c in self._http.cookies}
        data_file("cookies.json").write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8"
        )

    def clear_cookies(self) -> None:
        self._http.cookies.clear()
        try:
            data_file("cookies.json").unlink(missing_ok=True)
        except OSError:
            pass

    # ---------- 请求 ----------

    def request(self, method: str, url: str, retries: int = 3, **kwargs) -> requests.Response:
        kwargs.setdefault("timeout", 15)
        kwargs.setdefault("verify", certifi.where())
        # 敏感信息脱敏后记录参数
        safe_kwargs = {k: v for k, v in kwargs.items()
                       if k in ("params", "data")}
        log.debug("请求 %s %s %s", method, url, safe_kwargs)
        delay = 1.0
        last_exc: Exception | None = None
        for attempt in range(retries):
            try:
                with self._lock:
                    resp = self._http.request(method, url, **kwargs)
                log.debug("响应 %s %s -> %s %s", method, url,
                          resp.status_code, resp.text[:500].replace("\n", " "))
                if "Set-Cookie" in resp.headers:
                    log.debug("Set-Cookie: %s", resp.headers["Set-Cookie"][:300])
                return resp
            except requests.RequestException as exc:
                log.warning("请求异常 %s %s (第%d次): %s", method, url,
                            attempt + 1, exc)
                last_exc = exc
                if attempt < retries - 1:
                    time.sleep(delay)
                    delay = min(delay * 2, 30.0)
        raise last_exc  # type: ignore[misc]

    def get(self, url: str, **kwargs) -> requests.Response:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs) -> requests.Response:
        return self.request("POST", url, **kwargs)

    def get_json(self, url: str, **kwargs) -> dict:
        resp = self.get(url, **kwargs)
        return resp.json()

    def post_json(self, url: str, **kwargs) -> dict:
        resp = self.post(url, **kwargs)
        return resp.json()


# 全局单例：登录态全应用共享
default_session = Session()
