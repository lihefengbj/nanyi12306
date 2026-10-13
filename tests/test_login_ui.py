import os
import threading
import time
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QBuffer, QIODevice, Qt
from PySide6.QtGui import QImage
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel, QMainWindow, QStackedWidget

from core.auth import Passenger, QrStatus
from ui.login_page import LoginPage, QrWorker
from ui.main_window import MainWindow
from ui.task_page import TaskPage


class ConfirmedAuth:
    def __init__(self, fail=False):
        self.fail = fail
        self.get_user_name = Mock(side_effect=AssertionError("GUI must not fetch name"))
        self.get_passengers = Mock(return_value=[
            Passenger("Test passenger", "1", "0000000000001234", "", "1"),
        ])

    def create_qr(self):
        # One-pixel PNG; a valid image avoids unrelated image decoder warnings.
        image = QImage(1, 1, QImage.Format.Format_ARGB32)
        image.fill(0xffffffff)
        buffer = QBuffer()
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        image.save(buffer, "PNG")
        return "test-qr", bytes(buffer.data())

    def check_qr(self, uuid):
        return QrStatus.CONFIRMED, "test-ticket"

    def finish_login(self, ticket):
        if self.fail:
            raise RuntimeError("synthetic ticket exchange failure")


class LoginUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def wait_for(self, condition, timeout=3):
        deadline = time.monotonic() + timeout
        while not condition() and time.monotonic() < deadline:
            self.app.processEvents()
            QTest.qWait(5)
        self.app.processEvents()
        self.assertTrue(condition(), "Qt signal delivery timed out")

    def test_verified_login_updates_page_and_loads_passenger_widgets(self):
        auth = ConfirmedAuth()
        page = LoginPage(auth)
        task = TaskPage({"auth": auth, "stations": Mock(), "session": Mock()})
        # Construct only the window state needed by its real login callback.
        # This avoids startup network access, stations loading and tray creation.
        window = MainWindow.__new__(MainWindow)
        QMainWindow.__init__(window)
        window.auth = auth
        window.user_label = QLabel("未登录", window)
        window.stack = QStackedWidget(window)
        window.stack.addWidget(page)
        window.stack.addWidget(task)
        window.task_page = task
        page.sig_logged_in.connect(lambda: window._on_logged_in(""))

        try:
            page.start()
            self.wait_for(
                lambda: bool(task._passengers)
                and task._passenger_worker is None
                and not page._worker.isRunning()
            )
            self.assertEqual(page._status_label.text(), "登录成功")
            self.assertEqual(window.user_label.text(), "已登录")
            self.assertEqual(window.stack.currentWidget(), task)
            self.assertFalse(page._retry_button.isEnabled())
            self.assertTrue(task.refresh_passengers_btn.isEnabled())
            self.assertTrue(task.pass_hint.isHidden())
            self.assertEqual(task.pass_layout.itemAt(0).widget().text(),
                             "Test passenger（0000****1234）")
            auth.get_user_name.assert_not_called()
            auth.get_passengers.assert_called_once()
        finally:
            page._worker.wait(1000)
            if task._passenger_worker:
                task._passenger_worker.wait(1000)
            # Avoid MainWindow.closeEvent's tray-only production behavior.
            window.deleteLater()
            self.app.processEvents()

    def test_exchange_failure_replaces_waiting_and_does_not_emit_success(self):
        page = LoginPage(ConfirmedAuth(fail=True))
        successes = []
        page.sig_logged_in.connect(lambda: successes.append(True))
        try:
            with self.assertLogs("ny12306.login", level="ERROR"):
                page.start()
                self.wait_for(
                    lambda: not page._worker.isRunning()
                    and page._retry_button.isEnabled()
                )
            self.assertIn("扫码已确认，但登录会话建立失败",
                          page._status_label.text())
            self.assertNotEqual(page._status_label.text(), "等待扫码")
            self.assertEqual(successes, [])
        finally:
            page._worker.wait(1000)
            page.deleteLater()

    def test_qr_creation_failure_finishes_and_allows_manual_retry(self):
        auth = Mock()
        auth.create_qr.side_effect = RuntimeError("synthetic QR failure")
        page = LoginPage(auth)
        try:
            with self.assertLogs("ny12306.login", level="WARNING"):
                page.start()
                self.wait_for(
                    lambda: not page._worker.isRunning()
                    and page._retry_button.isEnabled()
                )
            self.assertIn("获取二维码失败", page._status_label.text())
            auth.create_qr.assert_called_once()
        finally:
            page._worker.wait(1000)
            page.deleteLater()

    def test_expired_qr_stops_without_automatic_replacement(self):
        auth = ConfirmedAuth()
        auth.create_qr = Mock(wraps=auth.create_qr)
        auth.check_qr = Mock(return_value=(QrStatus.EXPIRED, ""))
        auth.finish_login = Mock()
        page = LoginPage(auth)
        successes = []
        page.sig_logged_in.connect(lambda: successes.append(True))
        try:
            page.start()
            self.wait_for(
                lambda: not page._worker.isRunning()
                and page._retry_button.isEnabled()
            )
            self.assertIn("二维码已过期", page._status_label.text())
            self.assertNotIn("等待扫码", page._status_label.text())
            self.assertTrue(page._qr_label.pixmap().isNull())
            auth.create_qr.assert_called_once()
            auth.check_qr.assert_called_once()
            auth.finish_login.assert_not_called()
            self.assertEqual(successes, [])
        finally:
            page._worker.wait(1000)
            page.deleteLater()

    def test_manual_retry_after_expiry_creates_new_qr_then_logs_in(self):
        auth = ConfirmedAuth()
        auth.create_qr = Mock(wraps=auth.create_qr)
        auth.check_qr = Mock(side_effect=[
            (QrStatus.EXPIRED, ""), (QrStatus.CONFIRMED, "test-ticket"),
        ])
        page = LoginPage(auth)
        try:
            page.start()
            self.wait_for(
                lambda: not page._worker.isRunning()
                and page._retry_button.isEnabled()
            )
            self.assertEqual(auth.create_qr.call_count, 1)
            QTest.mouseClick(page._retry_button, Qt.MouseButton.LeftButton)
            self.wait_for(
                lambda: not page._worker.isRunning()
                and page._status_label.text() == "登录成功"
            )
            self.assertEqual(auth.create_qr.call_count, 2)
            self.assertFalse(page._retry_button.isEnabled())
            self.assertTrue(page._qr_label.pixmap().isNull())
        finally:
            page._worker.wait(1000)
            page.deleteLater()

    def test_timeout_stops_without_automatic_replacement(self):
        auth = ConfirmedAuth()
        auth.create_qr = Mock(wraps=auth.create_qr)
        auth.check_qr = Mock()
        page = LoginPage(auth)
        try:
            with patch.object(QrWorker, "WAIT_TIMEOUT_SECONDS", 0):
                page.start()
                self.wait_for(
                    lambda: not page._worker.isRunning()
                    and page._retry_button.isEnabled()
                )
            self.assertIn("等待扫码确认超时", page._status_label.text())
            self.assertTrue(page._qr_label.pixmap().isNull())
            auth.create_qr.assert_called_once()
            auth.check_qr.assert_not_called()
        finally:
            page._worker.wait(1000)
            page.deleteLater()

    def test_refresh_during_pending_poll_waits_and_discards_old_signals(self):
        auth = ConfirmedAuth()
        first_poll_started = threading.Event()
        allow_first_poll_to_finish = threading.Event()
        created = []

        def create_qr():
            number = len(created) + 1
            created.append(number)
            uuid, image = ConfirmedAuth.create_qr(auth)
            return f"test-qr-{number}", image

        def check_qr(uuid):
            if uuid == "test-qr-1":
                first_poll_started.set()
                if not allow_first_poll_to_finish.wait(3):
                    raise RuntimeError("synthetic blocked poll timeout")
                # Even an old CONFIRMED response arriving after a refresh must
                # not exchange that ticket or affect the next attempt.
                return QrStatus.CONFIRMED, "obsolete-ticket"
            return QrStatus.EXPIRED, ""

        auth.create_qr = create_qr
        auth.check_qr = check_qr
        auth.finish_login = Mock()
        page = LoginPage(auth)
        try:
            page.start()
            self.wait_for(lambda: first_poll_started.is_set()
                          and page._retry_button.isEnabled())
            old_worker = page._worker
            QTest.mouseClick(page._retry_button, Qt.MouseButton.LeftButton)
            self.assertTrue(page._restart_pending)
            self.assertTrue(page._qr_label.pixmap().isNull())
            self.assertEqual(created, [1])
            old_worker.sig_status.emit("obsolete status")
            old_worker.sig_qr.emit(old_worker.qr_image)
            self.app.processEvents()
            self.assertNotEqual(page._status_label.text(), "obsolete status")
            self.assertTrue(page._qr_label.pixmap().isNull())

            allow_first_poll_to_finish.set()
            self.wait_for(
                lambda: page._worker is not old_worker
                and not page._worker.isRunning()
                and page._retry_button.isEnabled()
            )
            self.assertEqual(created, [1, 2])
            self.assertIn("二维码已过期", page._status_label.text())
            # Already-finished workers may still have queued messages.
            old_worker.sig_logged_in.emit()
            old_worker.sig_login_error.emit("obsolete error")
            old_worker.sig_qr.emit(old_worker.qr_image)
            self.app.processEvents()
            self.assertIn("二维码已过期", page._status_label.text())
            self.assertTrue(page._qr_label.pixmap().isNull())
            auth.finish_login.assert_not_called()
        finally:
            allow_first_poll_to_finish.set()
            page.stop()
            page._worker.wait(1000)
            page.deleteLater()

    def test_small_qr_is_shown_at_original_resolution(self):
        auth = ConfirmedAuth()
        poll_started = threading.Event()
        finish_poll = threading.Event()

        def check_qr(uuid):
            poll_started.set()
            finish_poll.wait(3)
            return QrStatus.EXPIRED, ""

        auth.check_qr = check_qr
        page = LoginPage(auth)
        try:
            page.start()
            self.wait_for(lambda: poll_started.is_set()
                          and page._retry_button.isEnabled())
            self.assertEqual(page._qr_label.pixmap().width(), 1)
            self.assertEqual(page._qr_label.pixmap().height(), 1)
        finally:
            page.stop()
            finish_poll.set()
            page._worker.wait(1000)
            page.deleteLater()

    def test_cannot_replace_qr_during_ticket_exchange(self):
        auth = ConfirmedAuth()
        exchange_started = threading.Event()
        finish_exchange = threading.Event()

        def exchange(ticket):
            exchange_started.set()
            finish_exchange.wait(3)

        auth.finish_login = exchange
        auth.create_qr = Mock(wraps=auth.create_qr)
        page = LoginPage(auth)
        try:
            page.start()
            self.wait_for(lambda: exchange_started.is_set()
                          and not page._retry_button.isEnabled())
            page._request_new_qr()
            self.assertFalse(page._restart_pending)
            auth.create_qr.assert_called_once()
            finish_exchange.set()
            self.wait_for(lambda: not page._worker.isRunning()
                          and page._status_label.text() == "登录成功")
        finally:
            page.stop()
            finish_exchange.set()
            page._worker.wait(1000)
            page.deleteLater()

    def test_passenger_failure_is_visible_and_refresh_is_reenabled(self):
        auth = Mock()
        auth.get_passengers.side_effect = RuntimeError("登录会话未建立")
        task = TaskPage({"auth": auth, "stations": Mock(), "session": Mock()})
        try:
            with self.assertLogs("ny12306.passengers", level="WARNING"):
                task.load_passengers()
                self.wait_for(lambda: task._passenger_worker is None)
            self.assertIn("登录会话未建立", task.pass_hint.text())
            self.assertFalse(task.pass_hint.isHidden())
            self.assertTrue(task.refresh_passengers_btn.isEnabled())
            self.assertEqual(task._passengers, [])
        finally:
            if task._passenger_worker:
                task._passenger_worker.wait(1000)
            task.deleteLater()


if __name__ == "__main__":
    unittest.main()
