"""任务配置页：查询余票、勾选车次、设置席别优先级与乘车人，启动抢票。"""
import uuid

from PySide6.QtCore import QDate, Qt, QTime, QThread, Signal
from PySide6.QtWidgets import (
    QCheckBox, QDateEdit, QGroupBox, QHBoxLayout, QLabel,
    QListWidget, QListWidgetItem, QMessageBox, QPushButton, QScrollArea,
    QTimeEdit, QVBoxLayout, QWidget,
)

from core.auth import Auth, Passenger
from core.query import TicketFilter, TrainTicket, query_tickets
from core.worker import GrabTask

from .widgets import StationEdit


class QueryWorker(QThread):
    sig_result = Signal(list)
    sig_error = Signal(str)

    def __init__(self, session, date, from_code, to_code, parent=None):
        super().__init__(parent)
        self._args = (session, date, from_code, to_code)

    def run(self):
        try:
            trains = query_tickets(*self._args)
            self.sig_result.emit(trains)
        except Exception as exc:
            self.sig_error.emit(str(exc))


class PassengerWorker(QThread):
    sig_result = Signal(list)
    sig_error = Signal(str)

    def __init__(self, auth: Auth, parent=None):
        super().__init__(parent)
        self._auth = auth

    def run(self):
        try:
            self.sig_result.emit(self._auth.get_passengers())
        except Exception as exc:
            self.sig_error.emit(str(exc))


class TaskPage(QWidget):
    sig_start_task = Signal(object)  # GrabTask

    def __init__(self, app_ctx: dict, parent=None) -> None:
        super().__init__(parent)
        self._ctx = app_ctx  # {"session": Session, "stations": StationTable, "auth": Auth}
        self._trains: list[TrainTicket] = []
        self._passengers: list[Passenger] = []
        self._build_ui()

    # ---------- UI ----------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)

        # 行1：线路
        row1 = QHBoxLayout()
        self.from_edit = StationEdit(self._ctx["stations"])
        self.to_edit = StationEdit(self._ctx["stations"])
        self.from_edit.setPlaceholderText("出发地，如 北京南")
        self.to_edit.setPlaceholderText("目的地，如 上海虹桥")
        self.date_edit = QDateEdit(QDate.currentDate().addDays(1))
        self.date_edit.setCalendarPopup(True)
        self.date_edit.setDisplayFormat("yyyy-MM-dd")
        self.query_btn = QPushButton("查询余票")
        self.query_btn.clicked.connect(self._do_query)
        for w in (QLabel("出发地"), self.from_edit, QLabel("目的地"), self.to_edit,
                  QLabel("日期"), self.date_edit, self.query_btn):
            row1.addWidget(w)
        root.addLayout(row1)

        # 行2：车次列表
        self.train_list = QListWidget()
        self.train_list.setMinimumHeight(180)
        root.addWidget(QLabel("查询结果（勾选要抢的车次，不勾则全部监控）："))
        root.addWidget(self.train_list)

        # 行3：席别优先级 + 乘车人
        row3 = QHBoxLayout()
        seat_box = QGroupBox("席别优先级（勾选，按顺序尝试）")
        seat_layout = QVBoxLayout(seat_box)
        self.seat_checks: dict[str, QCheckBox] = {}
        for name in ["二等座", "一等座", "商务座", "硬卧", "软卧", "硬座", "无座"]:
            cb = QCheckBox(name)
            if name == "二等座":
                cb.setChecked(True)
            self.seat_checks[name] = cb
            seat_layout.addWidget(cb)
        row3.addWidget(seat_box)

        pass_box = QGroupBox("乘车人（需先登录）")
        pass_scroll = QScrollArea()
        pass_scroll.setWidgetResizable(True)
        pass_inner = QWidget()
        self.pass_layout = QVBoxLayout(pass_inner)
        pass_scroll.setWidget(pass_inner)
        pb_layout = QVBoxLayout(pass_box)
        pb_layout.addWidget(pass_scroll)
        self.pass_hint = QLabel("")
        self.pass_hint.setStyleSheet("color: #c62828;")
        self.pass_hint.setWordWrap(True)
        self.pass_hint.hide()
        pb_layout.addWidget(self.pass_hint)
        refresh_btn = QPushButton("刷新乘车人")
        refresh_btn.clicked.connect(self.load_passengers)
        pb_layout.addWidget(refresh_btn)
        row3.addWidget(pass_box)
        root.addLayout(row3)

        # 行4：选项
        row4 = QHBoxLayout()
        row4.addWidget(QLabel("出发时间窗"))
        self.time_from = QTimeEdit(QTime(0, 0))
        self.time_to = QTimeEdit(QTime(23, 59))
        row4.addWidget(self.time_from)
        row4.addWidget(QLabel("~"))
        row4.addWidget(self.time_to)
        self.cb_candidate = QCheckBox("无票时自动提交候补")
        self.cb_candidate.setChecked(True)
        self.cb_no_seat = QCheckBox("接受无座兜底")
        row4.addWidget(self.cb_candidate)
        row4.addWidget(self.cb_no_seat)
        row4.addStretch(1)
        root.addLayout(row4)

        # 行5：启动
        self.start_btn = QPushButton("▶ 开始抢票")
        self.start_btn.setStyleSheet("font-size: 16px; padding: 8px;")
        self.start_btn.clicked.connect(self._start_task)
        root.addWidget(self.start_btn)

    # ---------- 行为 ----------

    def load_passengers(self) -> None:
        self.pass_hint.hide()
        self.pass_hint.setText("")
        w = PassengerWorker(self._ctx["auth"], self)
        w.sig_result.connect(self._fill_passengers)
        w.sig_error.connect(self._passengers_error)
        w.start()

    def _fill_passengers(self, passengers: list[Passenger]) -> None:
        self._passengers = passengers
        while self.pass_layout.count():
            item = self.pass_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        for p in passengers:
            tail = p.id_no[-4:] if len(p.id_no) >= 8 else ""
            cb = QCheckBox(f"{p.name}（{p.id_no[:4]}****{tail}）")
            cb.setProperty("passenger", p)
            self.pass_layout.addWidget(cb)
        self.pass_layout.addStretch(1)

    def _passengers_error(self, msg: str) -> None:
        self.pass_hint.setText(f"乘车人加载失败：{msg}\n请确认已登录后点击「刷新乘车人」")
        self.pass_hint.show()

    def _do_query(self) -> None:
        from_code, to_code = self.from_edit.selected_code(), self.to_edit.selected_code()
        if not from_code or not to_code:
            QMessageBox.warning(self, "提示", "请从补全列表中选择有效的出发地和目的地")
            return
        self.query_btn.setEnabled(False)
        self.query_btn.setText("查询中…")
        w = QueryWorker(self._ctx["session"],
                        self.date_edit.date().toString("yyyy-MM-dd"),
                        from_code, to_code, self)
        w.sig_result.connect(self._fill_trains)
        w.sig_error.connect(self._query_error)
        w.start()

    def _fill_trains(self, trains: list[TrainTicket]) -> None:
        self.query_btn.setEnabled(True)
        self.query_btn.setText("查询余票")
        self._trains = trains
        self.train_list.clear()
        for t in trains:
            seat_info = "  ".join(
                f"{k}:{v}" for k, v in t.seats.items() if v and v != "无")
            item = QListWidgetItem(
                f"{t.code}  {t.start_time}→{t.arrive_time} ({t.duration})  {seat_info}")
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Unchecked)
            item.setData(Qt.ItemDataRole.UserRole, t.code)
            self.train_list.addItem(item)

    def _query_error(self, msg: str) -> None:
        self.query_btn.setEnabled(True)
        self.query_btn.setText("查询余票")
        QMessageBox.warning(self, "查询失败", msg)

    def _start_task(self) -> None:
        from_code, to_code = self.from_edit.selected_code(), self.to_edit.selected_code()
        if not from_code or not to_code:
            QMessageBox.warning(self, "提示", "请选择出发地和目的地")
            return
        checked_codes = []
        for i in range(self.train_list.count()):
            item = self.train_list.item(i)
            if item.checkState() == Qt.CheckState.Checked:
                checked_codes.append(item.data(Qt.ItemDataRole.UserRole))
        seat_priority = [name for name, cb in self.seat_checks.items() if cb.isChecked()]
        if not seat_priority and not self.cb_no_seat.isChecked():
            QMessageBox.warning(self, "提示", "请至少勾选一个席别")
            return
        passengers = []
        for i in range(self.pass_layout.count()):
            w = self.pass_layout.itemAt(i).widget()
            if isinstance(w, QCheckBox) and w.isChecked():
                passengers.append(w.property("passenger"))
        if not passengers:
            QMessageBox.warning(self, "提示", "请至少选择一名乘车人（需先登录）")
            return
        tf = TicketFilter(
            train_codes=checked_codes,
            seat_priority=seat_priority,
            time_from=self.time_from.time().toString("HH:mm"),
            time_to=self.time_to.time().toString("HH:mm"),
        )
        task = GrabTask(
            task_id=uuid.uuid4().hex[:6],
            train_date=self.date_edit.date().toString("yyyy-MM-dd"),
            from_code=from_code, from_name=self.from_edit.text().strip(),
            to_code=to_code, to_name=self.to_edit.text().strip(),
            filter=tf, passengers=passengers,
            auto_candidate=self.cb_candidate.isChecked(),
            accept_no_seat=self.cb_no_seat.isChecked(),
        )
        self.sig_start_task.emit(task)
