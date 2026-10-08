"""设置页：轮询间隔、通知渠道。"""
import logging
import os
import subprocess
import sys
from pathlib import Path

from PySide6.QtWidgets import (
    QCheckBox, QDoubleSpinBox, QFormLayout, QLabel, QLineEdit,
    QPushButton, QVBoxLayout, QWidget,
)

from core.config import MIN_POLL_INTERVAL, load_config, save_config
from core.logging_setup import log_file_path


class SettingsPage(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        cfg = load_config()
        form = QFormLayout()

        self.interval = QDoubleSpinBox()
        self.interval.setRange(MIN_POLL_INTERVAL, 30.0)
        self.interval.setSingleStep(0.5)
        self.interval.setSuffix(" 秒")
        self.interval.setValue(float(cfg["poll_interval"]))
        form.addRow("轮询间隔（下限 1.5s）", self.interval)

        self.jitter = QDoubleSpinBox()
        self.jitter.setRange(0.0, 10.0)
        self.jitter.setSingleStep(0.5)
        self.jitter.setSuffix(" 秒")
        self.jitter.setValue(float(cfg["poll_jitter"]))
        form.addRow("随机抖动", self.jitter)

        self.cb_popup = QCheckBox("桌面弹窗")
        self.cb_popup.setChecked(bool(cfg["notify_popup"]))
        self.cb_sound = QCheckBox("提示音")
        self.cb_sound.setChecked(bool(cfg["notify_sound"]))
        form.addRow("出票通知", self.cb_popup)
        form.addRow("", self.cb_sound)

        self.serverchan = QLineEdit(str(cfg["serverchan_key"]))
        self.serverchan.setPlaceholderText("Server酱 SendKey（可选）")
        form.addRow("Server酱", self.serverchan)

        self.bark = QLineEdit(str(cfg["bark_url"]))
        self.bark.setPlaceholderText("如 https://api.day.app/你的Key（可选）")
        form.addRow("Bark", self.bark)

        save_btn = QPushButton("保存设置")
        save_btn.clicked.connect(self._save)
        form.addRow(save_btn)

        log_path = log_file_path()
        log_label = QLabel(f"<a href='file:///{Path(log_path).parent.as_posix()}'>{log_path}</a>")
        log_label.setOpenExternalLinks(False)
        log_label.setWordWrap(True)
        log_label.setTextInteractionFlags(log_label.textInteractionFlags())
        form.addRow("日志文件", log_label)
        open_log_btn = QPushButton("打开日志目录（反馈问题时请附上日志）")
        open_log_btn.clicked.connect(self._open_log_dir)
        form.addRow(open_log_btn)

        root = QVBoxLayout(self)
        root.addLayout(form)
        root.addStretch(1)
        self._save_btn = save_btn

    def _open_log_dir(self) -> None:
        log_dir = str(Path(log_file_path()).parent)
        try:
            if sys.platform == "darwin":
                subprocess.Popen(["open", log_dir])
            elif os.name == "nt":
                os.startfile(log_dir)  # type: ignore[attr-defined]
            else:
                subprocess.Popen(["xdg-open", log_dir])
        except Exception as exc:
            logging.getLogger("ny12306.ui").warning("打开日志目录失败: %s", exc)

    def _save(self) -> None:
        save_config({
            "poll_interval": self.interval.value(),
            "poll_jitter": self.jitter.value(),
            "notify_popup": self.cb_popup.isChecked(),
            "notify_sound": self.cb_sound.isChecked(),
            "serverchan_key": self.serverchan.text().strip(),
            "bark_url": self.bark.text().strip(),
        })
        self._save_btn.setText("已保存 ✓")
