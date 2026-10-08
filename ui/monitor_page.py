"""监控页：任务卡片 + 实时余票 + 日志面板。"""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QGroupBox, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton,
    QScrollArea, QVBoxLayout, QWidget,
)

from core.worker import GrabWorker

STATUS_COLORS = {
    "监控中": "#2e7d32", "下单中": "#f9a825", "排队中": "#f9a825",
    "已下单": "#1565c0", "候补中": "#6a1b9a", "失败": "#c62828",
    "登录失效": "#c62828", "已停止": "#616161",
}


class TaskCard(QGroupBox):
    def __init__(self, worker: GrabWorker, on_stop, parent=None) -> None:
        super().__init__(worker.task.title(), parent)
        self.worker = worker
        layout = QVBoxLayout(self)
        row = QHBoxLayout()
        self.status_label = QLabel("监控中")
        self.status_label.setStyleSheet("font-weight: bold; color: #2e7d32;")
        self.poll_label = QLabel("轮询 0 次")
        stop_btn = QPushButton("停止")
        stop_btn.clicked.connect(lambda: on_stop(self))
        row.addWidget(self.status_label)
        row.addWidget(self.poll_label)
        row.addStretch(1)
        row.addWidget(stop_btn)
        layout.addLayout(row)
        self.ticket_label = QLabel("等待首次查询…")
        self.ticket_label.setWordWrap(True)
        layout.addWidget(self.ticket_label)

        worker.sig_status.connect(self.set_status)
        worker.sig_tickets.connect(self.update_tickets)

    def set_status(self, status: str) -> None:
        color = STATUS_COLORS.get(status, "#333")
        self.status_label.setText(status)
        self.status_label.setStyleSheet(f"font-weight: bold; color: {color};")
        self.poll_label.setText(f"轮询 {self.worker.poll_count} 次")

    def update_tickets(self, trains: list) -> None:
        f = self.worker.task.filter
        lines = []
        for t in trains:
            if f.train_codes and t.code not in f.train_codes:
                continue
            seat_info = "  ".join(
                f"{k}:{v}" for k, v in t.seats.items()
                if v and v != "无" and k in (f.seat_priority or list(t.seats)))
            lines.append(f"{t.code} {t.start_time}  {seat_info}")
        self.ticket_label.setText("\n".join(lines[:8]) or "暂无符合条件的车次")
        self.poll_label.setText(f"轮询 {self.worker.poll_count} 次")


class MonitorPage(QWidget):
    def __init__(self, on_success, parent=None) -> None:
        super().__init__(parent)
        self._on_success = on_success
        self._cards: list[TaskCard] = []

        root = QVBoxLayout(self)
        self.cards_area = QScrollArea()
        self.cards_area.setWidgetResizable(True)
        cards_inner = QWidget()
        self.cards_layout = QVBoxLayout(cards_inner)
        self.cards_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.cards_area.setWidget(cards_inner)
        root.addWidget(self.cards_area, 2)

        root.addWidget(QLabel("日志"))
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(2000)
        root.addWidget(self.log_view, 1)

    def add_worker(self, worker: GrabWorker) -> None:
        card = TaskCard(worker, self._stop_card)
        self._cards.append(card)
        self.cards_layout.addWidget(card)
        worker.sig_log.connect(self.append_log)
        worker.sig_success.connect(self._on_success)
        worker.start()

    def _stop_card(self, card: TaskCard) -> None:
        card.worker.stop()
        card.set_status("已停止")

    def append_log(self, line: str) -> None:
        self.log_view.appendPlainText(line)
        scrollbar = self.log_view.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())
