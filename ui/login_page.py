"""登录页：二维码展示与扫码状态机。"""
from PySide6.QtCore import QThread, Signal, Qt
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

from core.auth import Auth, QrStatus


class QrWorker(QThread):
    """后台轮询扫码状态。"""
    sig_qr = Signal(bytes)          # 二维码图片
    sig_status = Signal(QrStatus)
    sig_done = Signal(str)          # uamtk

    def __init__(self, auth: Auth, parent=None) -> None:
        super().__init__(parent)
        self._auth = auth
        self._stop = False

    def stop(self) -> None:
        self._stop = True

    def run(self) -> None:
        while not self._stop:
            try:
                uuid, img = self._auth.create_qr()
            except Exception as exc:
                self.sig_status.emit(QrStatus.EXPIRED)
                self.msleep(3000)
                continue
            self.sig_qr.emit(img)
            self.sig_status.emit(QrStatus.WAITING)
            for _ in range(60):  # 二维码约 60 秒有效期
                if self._stop:
                    return
                try:
                    status, uamtk = self._auth.check_qr(uuid)
                except Exception:
                    self.msleep(1000)
                    continue
                self.sig_status.emit(status)
                if status is QrStatus.CONFIRMED:
                    self.sig_done.emit(uamtk)
                    return
                if status is QrStatus.EXPIRED:
                    break
                self.msleep(1000)


class LoginPage(QWidget):
    sig_logged_in = Signal()

    def __init__(self, auth: Auth, parent=None) -> None:
        super().__init__(parent)
        self._auth = auth
        self._worker: QrWorker | None = None

        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title = QLabel("请使用 12306 App 扫码登录")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setStyleSheet("font-size: 18px;")
        self._qr_label = QLabel("二维码加载中…")
        self._qr_label.setFixedSize(240, 240)
        self._qr_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._qr_label.setStyleSheet("border: 1px solid #ccc;")
        self._status_label = QLabel("")
        self._status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)
        layout.addWidget(self._qr_label, alignment=Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self._status_label)

    def start(self) -> None:
        if self._worker and self._worker.isRunning():
            return
        self._worker = QrWorker(self._auth, self)
        self._worker.sig_qr.connect(self._show_qr)
        self._worker.sig_status.connect(
            lambda s: self._status_label.setText(s.value))
        self._worker.sig_done.connect(self._finish)
        self._worker.start()

    def stop(self) -> None:
        if self._worker:
            self._worker.stop()

    def _show_qr(self, img: bytes) -> None:
        qimg = QImage.fromData(img)
        self._qr_label.setPixmap(QPixmap.fromImage(qimg).scaled(
            230, 230, Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation))

    def _finish(self, uamtk: str) -> None:
        try:
            self._auth.finish_login(uamtk)
            self._status_label.setText("登录成功")
            self.sig_logged_in.emit()
        except Exception as exc:
            self._status_label.setText(f"登录失败: {exc}，二维码将自动刷新")
            self._worker = None
            self.start()
