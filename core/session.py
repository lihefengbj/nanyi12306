"""12306 HTTP 会话封装：统一请求头、重试、Cookie 持久化。"""
import json
import logging
import re
import threading
import time
from urllib.parse import urljoin, urlsplit, urlunsplit

import certifi
import requests

from .paths import data_file

log = logging.getLogger("ny12306.http")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

BASE_URL = "https://kyfw.12306.cn"


def _safe_url(url: str) -> str:
    """Keep scheme/host/path for diagnostics; never include credentials or query values."""
    parsed = urlsplit(url)
    if not parsed.scheme or not parsed.netloc:
        return parsed.path or "<unknown-url>"
    host = parsed.hostname or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    if parsed.port:
        host = f"{host}:{parsed.port}"
    return urlunsplit((parsed.scheme, host, parsed.path, "", ""))


def _field_names(value) -> str:
    """Return form field names without logging any submitted values."""
    if isinstance(value, dict):
        return ",".join(sorted(str(key) for key in value)) or "(none)"
    if isinstance(value, (list, tuple)):
        names = []
        for item in value:
            if isinstance(item, (list, tuple)) and item:
                names.append(str(item[0]))
        return ",".join(sorted(set(names))) or "(none)"
    return "(none)"


def _response_kind(response: requests.Response) -> str:
    content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
    raw = response.content[:512]
    if not raw:
        return "empty"

    prefix = raw.decode(response.encoding or "utf-8", errors="replace")
    prefix = prefix.lstrip("\ufeff \t\r\n").lower()
    if "html" in content_type or prefix.startswith(("<!doctype html", "<html")):
        if "xhtml" in content_type or "xhtml" in prefix[:256]:
            return "xhtml"
        return "html"
    if prefix.startswith(("{", "[")):
        return "json"
    if "json" in content_type:
        return "json"
    if "xml" in content_type or prefix.startswith(("<?xml", "<!doctype")):
        return "xml"
    if content_type.startswith("text/") or prefix:
        return "text"
    return content_type or "binary"


def _redirect_chain(response: requests.Response) -> str:
    """Describe redirect hops without query strings or fragments."""
    hops = []
    for previous in getattr(response, "history", ()):
        source = _safe_url(previous.url or "")
        location = previous.headers.get("Location", "")
        target = _safe_url(urljoin(previous.url or "", location)) if location else "(missing)"
        hops.append(f"{previous.status_code}:{source}->{target}")
    return "|".join(hops) or "(none)"


class JsonResponseError(RuntimeError):
    """Safe metadata for an endpoint response that could not be decoded as JSON."""

    def __init__(self, method: str, request_url: str,
                 response: requests.Response) -> None:
        self.method = method.upper()
        self.request_url = _safe_url(request_url)
        self.status_code = response.status_code
        self.content_type = response.headers.get("Content-Type", "(missing)")[:160]
        self.final_url = _safe_url(response.url or request_url)
        self.response_kind = _response_kind(response)
        super().__init__(
            f"{self.method} {self.request_url} 返回内容无法解析为 JSON"
            f"（HTTP {self.status_code}，Content-Type={self.content_type}，"
            f"最终 URL={self.final_url}，响应类型={self.response_kind}）"
        )


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
        # Queries and complete order flows may hold this lock across requests.
        self._lock = threading.RLock()
        self._query_url: str | None = None
        self._query_not_before = 0.0
        self._query_failures = 0
        self.order_pending = False
        self._order_not_before = 0.0
        self._order_failures = 0
        self.load_cookies()

    def transaction(self):
        """Serialize a multi-request business flow on the shared account."""
        return self._lock

    def wait_for_query(self) -> None:
        """All tasks share the same minimum interval and failure cooldown."""
        remaining = self._query_not_before - time.monotonic()
        if remaining > 0:
            time.sleep(remaining)
        self._query_not_before = time.monotonic() + 1.5

    def query_failed(self) -> None:
        self._query_failures += 1
        delay = min(3.0 * 2 ** min(self._query_failures - 1, 4), 30.0)
        self._query_not_before = max(self._query_not_before, time.monotonic() + delay)
        # Reload the advertised route on the next, delayed query.
        self._query_url = None

    def query_succeeded(self) -> None:
        self._query_failures = 0

    # ---------- Cookie 持久化 ----------

    def load_cookies(self) -> None:
        try:
            raw = json.loads(data_file("cookies.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(raw, dict) or raw.get("version") != 2:
            # Old name/value dictionaries cannot distinguish /passport and /otn
            # JSESSIONID values. Never restore them as domainless "supercookies".
            log.info("忽略缺少域名/路径的旧版 Cookie 缓存，需要重新扫码登录")
            return
        entries = raw.get("cookies")
        if not isinstance(entries, list):
            return
        restored = 0
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            domain = entry.get("domain", "")
            name, value = entry.get("name"), entry.get("value")
            path = entry.get("path", "")
            if (not isinstance(domain, str)
                    or not (domain.lstrip(".") == "12306.cn"
                            or domain.lstrip(".").endswith(".12306.cn"))
                    or not isinstance(name, str) or not name
                    or not isinstance(value, str)
                    or not isinstance(path, str) or not path.startswith("/")):
                continue
            expires = entry.get("expires")
            if expires is not None and (
                    not isinstance(expires, (int, float)) or expires <= time.time()):
                continue
            rest = entry.get("rest")
            cookie = requests.cookies.create_cookie(
                name=name, value=value, domain=domain, path=path,
                secure=bool(entry.get("secure")), expires=expires,
                rest=rest if isinstance(rest, dict) else {},
            )
            cookie.domain_specified = bool(entry.get("domain_specified", True))
            cookie.domain_initial_dot = domain.startswith(".")
            self._http.cookies.set_cookie(cookie)
            restored += 1
        log.debug("恢复 scoped Cookie 数量=%d", restored)

    def save_cookies(self) -> None:
        with self._lock:
            entries = [
                {
                    "name": c.name, "value": c.value, "domain": c.domain,
                    "path": c.path, "secure": c.secure, "expires": c.expires,
                    "domain_specified": c.domain_specified, "rest": c._rest,
                }
                for c in self._http.cookies if c.domain and not c.is_expired()
            ]
        data = {"version": 2, "cookies": entries}
        data_file("cookies.json").write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8"
        )

    def clear_cookies(self) -> None:
        with self._lock:
            self._http.cookies.clear()
            self._query_url = None
            self.order_pending = False
        try:
            data_file("cookies.json").unlink(missing_ok=True)
        except OSError:
            pass

    # ---------- 请求 ----------

    def request(self, method: str, url: str, retries: int | None = None,
                **kwargs) -> requests.Response:
        # A timed-out POST may already have changed server state. Never replay
        # it transparently; the owning business flow must decide what is safe.
        if retries is None:
            retries = 3 if method.upper() in ("GET", "HEAD", "OPTIONS") else 1
        if retries < 1:
            raise ValueError("retries 必须至少为 1")
        kwargs.setdefault("timeout", 15)
        kwargs.setdefault("verify", certifi.where())
        safe_url = _safe_url(url)
        log.debug(
            "请求 %s %s params_fields=%s data_fields=%s",
            method, safe_url, _field_names(kwargs.get("params")),
            _field_names(kwargs.get("data")),
        )
        delay = 1.0
        last_exc: Exception | None = None
        for attempt in range(retries):
            try:
                with self._lock:
                    resp = self._http.request(method, url, **kwargs)
                content_type = resp.headers.get("Content-Type", "(missing)")[:160]
                log.debug(
                    "响应 %s %s -> HTTP %s content_type=%s kind=%s final_url=%s "
                    "set_cookie=%s redirects=%s",
                    method, safe_url, resp.status_code, content_type,
                    _response_kind(resp), _safe_url(resp.url or url),
                    "yes" if "Set-Cookie" in resp.headers else "no",
                    _redirect_chain(resp),
                )
                if "/passport/" in url or "/otn/login/" in url:
                    log.debug(
                        "会话 Cookie scopes=%s",
                        [(c.name, c.domain or "(unscoped)", c.path)
                         for c in getattr(self._http, "cookies", ())],
                    )
                return resp
            except requests.RequestException as exc:
                log.warning("请求异常 %s %s (第%d次): %s",
                            method, safe_url, attempt + 1, type(exc).__name__)
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
        try:
            return resp.json()
        except ValueError as exc:
            raise JsonResponseError("GET", url, resp) from exc

    def post_json(self, url: str, **kwargs) -> dict:
        resp = self.post(url, **kwargs)
        try:
            return resp.json()
        except ValueError as exc:
            raise JsonResponseError("POST", url, resp) from exc

    def post_jsonp(self, url: str, callback: str, **kwargs) -> dict:
        """POST to a same-origin JSONP endpoint and decode its named callback."""
        if not re.fullmatch(r"[A-Za-z_$][A-Za-z0-9_$]*", callback):
            raise ValueError("JSONP callback 必须是合法标识符")
        params = dict(kwargs.pop("params", {}) or {})
        params["callback"] = callback
        response = self.post(url, params=params, **kwargs)
        pattern = re.compile(
            rf"\s*(?:/\*\*/\s*)?{re.escape(callback)}\s*\((.*)\)\s*;?\s*",
            re.DOTALL,
        )
        match = pattern.fullmatch(response.text)
        if not match:
            raise JsonResponseError("POST", url, response)
        try:
            data = json.loads(match.group(1))
        except ValueError as exc:
            raise JsonResponseError("POST", url, response) from exc
        if not isinstance(data, dict):
            raise JsonResponseError("POST", url, response)
        return data


# 全局单例：登录态全应用共享
default_session = Session()
