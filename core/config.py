"""配置与任务模板的本地持久化（JSON）。"""
import json
import threading
from pathlib import Path
from typing import Any

from .paths import data_file

_lock = threading.Lock()

DEFAULTS: dict[str, Any] = {
    "poll_interval": 2.0,      # 轮询基础间隔（秒），下限 1.5
    "poll_jitter": 1.0,        # 随机抖动范围
    "notify_popup": True,
    "notify_sound": True,
    "serverchan_key": "",
    "bark_url": "",
    "auto_wait_sale_time": False,  # 按起售时间自动开始
}

MIN_POLL_INTERVAL = 1.5


def _load(path: Path, default: Any) -> Any:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        pass
    return default


def _save(path: Path, data: Any) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def load_config() -> dict[str, Any]:
    with _lock:
        cfg = DEFAULTS | (_load(data_file("config.json"), {}) or {})
    cfg["poll_interval"] = max(MIN_POLL_INTERVAL, float(cfg.get("poll_interval", 2.0)))
    return cfg


def save_config(cfg: dict[str, Any]) -> None:
    cfg["poll_interval"] = max(MIN_POLL_INTERVAL, float(cfg.get("poll_interval", 2.0)))
    with _lock:
        _save(data_file("config.json"), cfg)


def load_tasks() -> list[dict[str, Any]]:
    with _lock:
        return _load(data_file("tasks.json"), []) or []


def save_tasks(tasks: list[dict[str, Any]]) -> None:
    with _lock:
        _save(data_file("tasks.json"), tasks)
