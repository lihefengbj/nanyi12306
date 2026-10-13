"""一键构建脚本：自动识别平台调用 PyInstaller。

Windows -> dist/nanyi12306.exe
macOS   -> dist/nanyi12306.app
"""
import platform
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def resource_path(rel: str) -> str:
    sep = ";" if platform.system() == "Windows" else ":"
    return f"{ROOT / 'assets' / rel}{sep}assets/{rel}"


def main() -> int:
    system = platform.system()
    args = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean", "--windowed",
        "--name", "nanyi12306",
        str(ROOT / "main.py"),
    ]
    if (ROOT / "assets").is_dir():
        args.insert(args.index(str(ROOT / "main.py")), "--add-data")
        args.insert(args.index(str(ROOT / "main.py")), resource_path(""))
    if system == "Windows":
        args.insert(args.index("--windowed") + 1, "--onefile")
        if (ROOT / "assets" / "app.ico").exists():
            args += ["--icon", str(ROOT / "assets" / "app.ico")]
    elif system == "Darwin":
        if (ROOT / "assets" / "app.icns").exists():
            args += ["--icon", str(ROOT / "assets" / "app.icns")]
        args += ["--osx-bundle-identifier", "com.nanyi12306.app"]
    else:
        print(f"不支持的平台: {system}")
        return 1
    print("运行:", " ".join(args))
    return subprocess.call(args, cwd=ROOT)


if __name__ == "__main__":
    sys.exit(main())
