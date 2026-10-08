"""用户数据目录管理（跨平台）。

Windows: %APPDATA%/nanyi12306/
macOS:   ~/Library/Application Support/nanyi12306/
"""
from pathlib import Path

from platformdirs import user_data_dir

APP_NAME = "nanyi12306"


def data_dir() -> Path:
    d = Path(user_data_dir(APP_NAME, ensure_exists=True))
    return d


def data_file(name: str) -> Path:
    return data_dir() / name
