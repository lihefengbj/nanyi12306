"""日志模块：滚动文件日志 + 控制台输出。

日志文件: <数据目录>/logs/nanyi12306.log（单文件 2MB，保留 5 份）
记录接口元信息（状态码、Content-Type、最终 URL、响应类型），不记录请求值、Cookie 或响应正文。
"""
import logging
from logging.handlers import RotatingFileHandler

from .paths import data_dir

_initialized = False


def setup_logging(level: int = logging.DEBUG) -> str:
    """初始化全局日志，返回日志文件路径。可重复调用（幂等）。"""
    global _initialized
    log_dir = data_dir() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "nanyi12306.log"
    if _initialized:
        return str(log_file)

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(level)

    fh = RotatingFileHandler(log_file, maxBytes=2 * 1024 * 1024,
                             backupCount=5, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)

    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)
    root.addHandler(ch)

    _initialized = True
    logging.getLogger(__name__).info("日志初始化: %s", log_file)
    return str(log_file)


def log_file_path() -> str:
    return str(data_dir() / "logs" / "nanyi12306.log")
