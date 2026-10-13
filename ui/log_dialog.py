"""运行日志查看窗口。"""
from pathlib import Path

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QMessageBox, QPlainTextEdit,
    QPushButton, QVBoxLayout, QApplication,
)

from core.logging_setup import log_file_path


class LogDialog(QDialog):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("运行日志")
        self.resize(900, 600)

        self._log_path = Path(log_file_path())
        self._path_label = QLabel(str(self._log_path))
        self._path_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self._warning_label = QLabel(
            "提示：日志可能包含个人信息；旧版本日志还可能包含敏感参数，分享前请检查并脱敏。"
        )
        self._log_view = QPlainTextEdit()
        self._log_view.setReadOnly(True)
        self._log_view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)

        refresh_button = QPushButton("刷新")
        refresh_button.clicked.connect(self._load_log)
        copy_button = QPushButton("复制日志")
        copy_button.clicked.connect(self._copy_log)
        folder_button = QPushButton("打开日志目录")
        folder_button.clicked.connect(self._open_log_folder)
        close_button = QPushButton("关闭")
        close_button.clicked.connect(self.accept)

        buttons = QHBoxLayout()
        buttons.addWidget(refresh_button)
        buttons.addWidget(copy_button)
        buttons.addWidget(folder_button)
        buttons.addStretch(1)
        buttons.addWidget(close_button)

        layout = QVBoxLayout(self)
        layout.addWidget(self._path_label)
        layout.addWidget(self._warning_label)
        layout.addWidget(self._log_view, 1)
        layout.addLayout(buttons)

        self._load_log()

    def _load_log(self) -> None:
        try:
            text = self._log_path.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            text = "日志文件尚未生成。"
        except OSError as exc:
            text = f"读取日志失败：{exc}"
        self._log_view.setPlainText(text)
        cursor = self._log_view.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        self._log_view.setTextCursor(cursor)

    def _copy_log(self) -> None:
        QApplication.clipboard().setText(self._log_view.toPlainText())

    def _open_log_folder(self) -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._log_path.parent)))
