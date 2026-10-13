"""主窗口：左侧导航 + 页面切换 + 状态栏 + 系统托盘。"""
from PySide6.QtCore import QThread
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QMainWindow,
    QMenu, QStackedWidget, QSystemTrayIcon, QWidget,
)

from core.auth import Auth
from core.config import load_config
from core.notify import notify_all
from core.session import Session
from core.stations import StationTable
from core.worker import GrabTask, GrabWorker

from .login_page import LoginPage
from .log_dialog import LogDialog
from .monitor_page import MonitorPage
from .settings_page import SettingsPage
from .task_page import TaskPage


class StationLoader(QThread):
    def __init__(self, stations: StationTable, parent=None) -> None:
        super().__init__(parent)
        self._stations = stations

    def run(self) -> None:
        try:
            self._stations.load()
        except Exception:
            pass


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("nanyi12306 - 火车票自动购票")
        self.resize(960, 640)

        log_menu = self.menuBar().addMenu("日志(&L)")
        view_log_action = QAction("查看运行日志", self)
        view_log_action.triggered.connect(self._show_logs)
        log_menu.addAction(view_log_action)

        # Core 上下文
        self.session = Session()
        self.auth = Auth(self.session)
        self.stations = StationTable(self.session)
        self.ctx = {"session": self.session, "auth": self.auth,
                    "stations": self.stations}
        self._workers: list[GrabWorker] = []

        # 页面
        self.stack = QStackedWidget()
        self.login_page = LoginPage(self.auth)
        self.task_page = TaskPage(self.ctx)
        self.monitor_page = MonitorPage(self._on_order_success)
        self.settings_page = SettingsPage()
        for page in (self.login_page, self.task_page,
                     self.monitor_page, self.settings_page):
            self.stack.addWidget(page)

        # 导航
        nav = QListWidget()
        nav.setFixedWidth(120)
        for name in ("登录", "抢票任务", "监控", "设置"):
            nav.addItem(QListWidgetItem(name))
        nav.currentRowChanged.connect(self._on_nav)

        central = QWidget()
        layout = QHBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(nav)
        layout.addWidget(self.stack, 1)
        self.setCentralWidget(central)

        # 状态栏
        self.user_label = QLabel("未登录")
        self.statusBar().addPermanentWidget(self.user_label)
        self.statusBar().showMessage("就绪")

        # 托盘
        self.tray = QSystemTrayIcon(self)
        self.tray.setIcon(self.style().standardIcon(
            self.style().StandardPixmap.SP_ComputerIcon))
        tray_menu = QMenu()
        show_action = QAction("显示窗口", self)
        show_action.triggered.connect(self.showNormal)
        quit_action = QAction("退出", self)
        quit_action.triggered.connect(self._quit)
        tray_menu.addAction(show_action)
        tray_menu.addAction(quit_action)
        self.tray.setContextMenu(tray_menu)
        self.tray.show()

        # 信号
        self.login_page.sig_logged_in.connect(lambda: self._on_logged_in(""))
        self.task_page.sig_start_task.connect(self._start_task)

        # 启动：加载车站表、尝试恢复登录
        self._station_loader = StationLoader(self.stations, self)
        self._station_loader.start()
        nav.setCurrentRow(0)
        self._try_resume_login()

    # ---------- 登录 ----------

    def _on_nav(self, row: int) -> None:
        self.stack.setCurrentIndex(row)
        # 切到任务页且乘车人列表为空时，自动重试加载
        if row == 1 and not self.task_page._passengers \
                and self.user_label.text().startswith("已登录"):
            self.task_page.load_passengers()

    def _try_resume_login(self) -> None:
        if self.auth.check_user():
            name = self.auth.get_user_name()
            self._on_logged_in(name)
        else:
            self.login_page.start()

    def _on_logged_in(self, name: str = "") -> None:
        # Session verification already succeeded in the QR thread. Do not
        # block the GUI on an optional username lookup before updating state.
        self.user_label.setText(f"已登录: {name}" if name else "已登录")
        self.stack.setCurrentIndex(1)
        self.task_page.load_passengers()

    # ---------- 任务 ----------

    def _start_task(self, task: GrabTask) -> None:
        worker = GrabWorker(task, self.session, self)
        self._workers.append(worker)
        self.monitor_page.add_worker(worker)
        self.stack.setCurrentIndex(2)
        self.statusBar().showMessage(
            f"任务 {task.task_id} 已启动，共 {len(self._workers)} 个")

    def _on_order_success(self, order_id: str, summary: str) -> None:
        cfg = load_config()
        notify_all(
            cfg,
            title="nanyi12306 下单成功",
            body=f"订单 {order_id}：{summary}\n请 10 分钟内到 12306 App 支付！",
            tray=self.tray,
        )
        self.statusBar().showMessage(f"下单成功: {order_id}")

    def _show_logs(self) -> None:
        LogDialog(self).exec()

    # ---------- 关闭 ----------

    def closeEvent(self, event) -> None:
        self.hide()
        self.tray.showMessage("nanyi12306", "已最小化到托盘，抢票任务继续运行",
                              self.tray.icon(), 3000)
        event.ignore()

    def _quit(self) -> None:
        for w in self._workers:
            w.stop()
        self.login_page.stop()
        from PySide6.QtWidgets import QApplication
        QApplication.quit()
