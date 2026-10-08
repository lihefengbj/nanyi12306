"""跨平台通知：桌面弹窗（托盘）+ 声音 + 手机推送（Server酱/Bark）。

桌面与声音部分依赖 Qt，仅在 GUI 线程使用；推送为纯 HTTP，可独立使用。
"""
import logging

import requests

log = logging.getLogger(__name__)


def push_serverchan(key: str, title: str, body: str) -> bool:
    if not key:
        return False
    try:
        resp = requests.post(
            f"https://sctapi.ftqq.com/{key}.send",
            data={"title": title, "desp": body}, timeout=10,
        )
        return resp.json().get("code") == 0
    except Exception as exc:
        log.warning("Server酱推送失败: %s", exc)
        return False


def push_bark(base_url: str, title: str, body: str) -> bool:
    if not base_url:
        return False
    try:
        url = f"{base_url.rstrip('/')}/{title}/{body}"
        resp = requests.get(url, timeout=10)
        return resp.status_code == 200
    except Exception as exc:
        log.warning("Bark 推送失败: %s", exc)
        return False


def notify_all(cfg: dict, title: str, body: str,
               tray=None, sound_effect=None) -> None:
    """统一入口：桌面弹窗 + 声音 + 配置的推送渠道。"""
    if cfg.get("notify_popup") and tray is not None:
        tray.showMessage(title, body, tray.icon(), 8000)
    if cfg.get("notify_sound") and sound_effect is not None:
        sound_effect.play()
    push_serverchan(cfg.get("serverchan_key", ""), title, body)
    push_bark(cfg.get("bark_url", ""), title, body)
