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
        print(f"Unsupported platform: {system}")
        return 1
    # CI runner consoles may be non-UTF-8 (e.g. cp1252); keep output ASCII-safe.
    print("Run:", " ".join(args))
    return subprocess.call(args, cwd=ROOT)


if __name__ == "__main__":
    sys.exit(main())
