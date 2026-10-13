"""nanyi12306 应用入口。"""
import logging
import sys

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from core.logging_setup import setup_logging


def main() -> int:
    log_file = setup_logging()
    logging.getLogger("ny12306").info(
        "应用启动，登录流程=scoped-cookie-passport-v2，日志文件: %s", log_file)
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(sys.argv)
    app.setApplicationName("nanyi12306")
    app.setQuitOnLastWindowClosed(False)  # 关窗进托盘，任务继续

    from ui.main_window import MainWindow
    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
