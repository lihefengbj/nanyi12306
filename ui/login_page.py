"""登录页：二维码展示与扫码状态机。"""
import logging
import time

from PySide6.QtCore import QThread, Signal, Slot, Qt
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import QLabel, QPushButton, QVBoxLayout, QWidget

from core.auth import Auth, QrStatus

log = logging.getLogger("ny12306.login")


class QrWorker(QThread):
    """后台轮询扫码状态。"""
    WAIT_TIMEOUT_SECONDS = 60.0
    MAX_POLLS = 60

    sig_qr = Signal(bytes)          # 二维码图片
    sig_status = Signal(str)
    sig_qr_invalidated = Signal(str)
    sig_logged_in = Signal()
    sig_login_error = Signal(str)

    def __init__(self, auth: Auth, parent=None) -> None:
        super().__init__(parent)
        self._auth = auth
        self._stop = False
        self.qr_image: bytes | None = None
        self.exchanging_ticket = False

    def stop(self) -> None:
        self._stop = True

    def run(self) -> None:
        if self._stop:
            return
        # Each explicit retry creates exactly one QR. Never silently replace an
        # expired image while the phone may still be confirming the old one.
        try:
            uuid, img = self._auth.create_qr()
        except Exception as exc:
            log.warning("获取登录二维码失败: %s", exc)
            self.sig_status.emit(f"获取二维码失败：{exc}。请点击「重新获取二维码」重试。")
            return
        if self._stop:
            return
        started = time.monotonic()
        self.qr_image = img
        self.sig_qr.emit(img)
        self.sig_status.emit("等待扫描当前二维码，请在 12306 App 中确认登录")
        log.info("二维码轮询开始；本轮不会自动换码")
        last_error = ""
        for poll in range(self.MAX_POLLS):
            if self._stop:
                return
            if time.monotonic() - started >= self.WAIT_TIMEOUT_SECONDS:
                break
            try:
                status, uamtk = self._auth.check_qr(uuid)
            except Exception as exc:
                if self._stop:
                    return
                message = f"二维码状态查询失败，正在重试：{exc}"
                if message != last_error:
                    log.warning("%s", message)
                    self.sig_status.emit(message)
                    last_error = message
                self.msleep(1000)
                continue
            if self._stop:
                return
            last_error = ""
            log.debug("扫码轮询 poll=%d elapsed_seconds=%.1f state=%s",
                      poll + 1, time.monotonic() - started, status.name)
            self.sig_status.emit(status.value)
            if status is QrStatus.EXPIRED:
                log.info("本轮二维码已过期；停止轮询，等待用户重新获取")
                self.sig_qr_invalidated.emit(
                    "二维码已过期，本轮登录未完成。"
                    "请点击「重新获取二维码」，重新扫描新码并在手机上确认。"
                )
                return
            if status is QrStatus.CONFIRMED:
                self.exchanging_ticket = True
                self.sig_qr_invalidated.emit("扫码已确认，正在建立登录会话…")
                try:
                    # Only verified business login, not the scan result itself,
                    # may emit success to the task page.
                    self._auth.finish_login(uamtk)
                except Exception as exc:
                    log.exception("扫码已确认，但建立登录会话失败")
                    if not self._stop:
                        self.sig_login_error.emit(str(exc))
                else:
                    if self._stop:
                        return
                    self.sig_logged_in.emit()
                return
            self.msleep(1000)
        if not self._stop:
            log.info("本轮扫码等待超时；未收到服务端确认，不自动换码")
            self.sig_qr_invalidated.emit(
                "等待扫码确认超时，未收到服务端确认。"
                "请点击「重新获取二维码」，重新扫描新码并在手机上确认。"
            )


class LoginPage(QWidget):
    sig_logged_in = Signal()

    def __init__(self, auth: Auth, parent=None) -> None:
        super().__init__(parent)
        self._auth = auth
        self._worker: QrWorker | None = None
        self._restart_pending = False
        self._attempt = 0

        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title = QLabel("请使用 12306 App 扫码登录")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setStyleSheet("font-size: 18px;")
        self._qr_label = QLabel("二维码加载中…")
        self._qr_label.setFixedSize(240, 240)
        self._qr_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._qr_label.setStyleSheet(
            "border: 1px solid #ccc; background: white; color: black;")
        self._status_label = QLabel("未登录，请扫码并在手机上确认")
        self._status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._status_label.setWordWrap(True)
        self._retry_button = QPushButton("重新获取二维码")
        self._retry_button.clicked.connect(self._request_new_qr)
        layout.addWidget(title)
        layout.addWidget(self._qr_label, alignment=Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self._status_label)
        layout.addWidget(self._retry_button, alignment=Qt.AlignmentFlag.AlignCenter)

    def start(self) -> None:
        if self._worker and self._worker.isRunning():
            return
        self._restart_pending = False
        self._attempt += 1
        self._worker = QrWorker(self._auth, self)
        self._worker.sig_qr.connect(
            self._show_qr, Qt.ConnectionType.QueuedConnection)
        self._worker.sig_status.connect(
            self._show_status, Qt.ConnectionType.QueuedConnection)
        self._worker.sig_qr_invalidated.connect(
            self._invalidate_qr, Qt.ConnectionType.QueuedConnection)
        self._worker.sig_logged_in.connect(
            self._finish, Qt.ConnectionType.QueuedConnection)
        self._worker.sig_login_error.connect(
            self._login_error, Qt.ConnectionType.QueuedConnection)
        self._worker.finished.connect(self._worker_finished)
        self._retry_button.setEnabled(False)
        self._qr_label.clear()
        self._qr_label.setText("正在获取新二维码…")
        self._status_label.setText("正在获取二维码…")
        self._worker.start()

    @Slot()
    def _request_new_qr(self) -> None:
        worker = self._worker
        if worker and worker.isRunning():
            if worker.exchanging_ticket:
                return
            # A phone-side expiry can precede the polling endpoint's expiry.
            # Wait asynchronously for the old request to finish before issuing
            # a replacement, keeping the shared Auth/session strictly sequential.
            self._restart_pending = True
            worker.stop()
            self._retry_button.setEnabled(False)
            self._qr_label.clear()
            self._qr_label.setText("正在取消旧二维码…")
            self._status_label.setText("正在结束旧码轮询，随后获取新二维码…")
            log.info("用户请求换码 attempt=%d；先停止旧轮询，不并发创建", self._attempt)
            return
        self.start()

    def stop(self) -> None:
        self._restart_pending = False
        if self._worker:
            self._worker.stop()

    def _accept_worker_signal(self) -> bool:
        sender = self.sender()
        return (self._worker is not None and sender is self._worker
                and not self._restart_pending
                and not self._worker._stop)

    @Slot(bytes)
    def _show_qr(self, img: bytes) -> None:
        if not self._accept_worker_signal():
            return
        matches_created = img == self._worker.qr_image
        if not matches_created:
            log.error("二维码显示拒绝: 图片不属于当前获取批次 attempt=%d", self._attempt)
            self._worker.stop()
            self._qr_label.clear()
            self._status_label.setText("二维码批次不一致，请重新获取二维码")
            return
        qimg = QImage.fromData(img)
        if qimg.isNull():
            log.error("二维码显示拒绝: 图片格式无效 attempt=%d", self._attempt)
            self._worker.stop()
            self._qr_label.clear()
            self._status_label.setText("二维码图片无效，请重新获取二维码")
            return
        # Keep the original modules when the image fits; do not interpolate
        # dark/light pixels just to fill the label. White background adds margin.
        pixmap = QPixmap.fromImage(qimg)
        if pixmap.width() > 230 or pixmap.height() > 230:
            pixmap = pixmap.scaled(
                230, 230, Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.FastTransformation)
        self._qr_label.setPixmap(pixmap)
        self._retry_button.setEnabled(not self._worker.exchanging_ticket)
        log.info(
            "二维码显示成功 attempt=%d image_matches_created=%s "
            "source_size=%dx%d display_size=%dx%d",
            self._attempt, matches_created, qimg.width(), qimg.height(),
            pixmap.width(), pixmap.height(),
        )

    @Slot(str)
    def _show_status(self, status: str) -> None:
        if not self._accept_worker_signal():
            return
        self._status_label.setText(status)

    @Slot(str)
    def _invalidate_qr(self, message: str) -> None:
        if not self._accept_worker_signal():
            return
        self._qr_label.clear()
        self._qr_label.setText(
            "二维码已确认" if message.startswith("扫码已确认") else "本轮二维码已失效"
        )
        self._status_label.setText(message)
        self._retry_button.setEnabled(False)

    @Slot()
    def _worker_finished(self) -> None:
        if self.sender() is not self._worker:
            return
        if self._restart_pending:
            self._restart_pending = False
            self.start()
            return
        self._retry_button.setEnabled(self._status_label.text() != "登录成功")

    @Slot()
    def _finish(self) -> None:
        if not self._accept_worker_signal():
            return
        self._qr_label.clear()
        self._qr_label.setText("已登录")
        self._status_label.setText("登录成功")
        self._retry_button.setEnabled(False)
        log.info("界面状态: 登录成功，通知任务页加载乘车人")
        self.sig_logged_in.emit()

    @Slot(str)
    def _login_error(self, message: str) -> None:
        if not self._accept_worker_signal():
            return
        log.info("界面状态: 扫码确认但业务会话建立失败")
        self._status_label.setText(
            f"扫码已确认，但登录会话建立失败：{message}。"
            "请从日志菜单查看失败接口，点击「重新获取二维码」重试。"
        )
