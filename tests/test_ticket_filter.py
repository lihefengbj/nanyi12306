import os
import threading
import time
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QTime, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from core.auth import Passenger
from core.query import TicketFilter, TrainTicket, apply_filter
from core.stations import StationTable
from core.worker import GrabTask, GrabWorker
from ui.monitor_page import TaskCard
from ui.task_page import QueryWorker, TaskPage


def train(code, departure, seats=None, can_buy=True):
    return TrainTicket(
        "synthetic-order-token", f"train-{code}", code, "出发站", "到达站",
        departure, "23:59", "01:00", "2026-10-16", can_buy,
        {"二等座": "有"} if seats is None else seats,
    )


def task(ticket_filter, **options):
    return GrabTask(
        task_id="test", train_date="2026-10-16",
        from_code="AAA", from_name="出发站",
        to_code="BBB", to_name="到达站",
        filter=ticket_filter, **options,
    )


class TicketFilterTests(unittest.TestCase):
    def test_departure_window_includes_both_endpoints(self):
        trains = [
            train("G1", "05:19"), train("G2", "17:59"),
            train("G3", "18:00"), train("G4", "18:30"),
            train("G5", "18:59"), train("G6", "19:00"),
        ]
        flt = TicketFilter(time_from="18:00", time_to="18:59",
                           only_available=False)
        self.assertEqual([t.code for t in apply_filter(trains, flt)],
                         ["G3", "G4", "G5"])

    def test_full_day_and_same_minute_windows(self):
        trains = [train("G1", "00:00"), train("G2", "18:00"),
                  train("G3", "23:59")]
        flt = TicketFilter(time_from="00:00", time_to="23:59",
                           only_available=False)
        self.assertEqual(apply_filter(trains, flt), trains)
        flt.time_from = flt.time_to = "18:00"
        self.assertEqual([t.code for t in apply_filter(trains, flt)], ["G2"])

    def test_train_match_ignores_availability_but_full_match_checks_seats(self):
        sold_out = train("G1", "18:30", {"二等座": "无"})
        flt = TicketFilter(train_codes=["G1"], seat_priority=["二等座"],
                           time_from="18:00", time_to="18:59")
        self.assertTrue(flt.match_train(sold_out))
        self.assertFalse(flt.match(sold_out))
        self.assertFalse(flt.match_train(train("G2", "18:30")))
        self.assertTrue(flt.match(train("G1", "18:30")))
        flt.only_available = False
        self.assertTrue(flt.match(sold_out))

    def test_grab_only_attempts_matching_departures_and_codes(self):
        flt = TicketFilter(train_codes=["G1", "G2"], seat_priority=["二等座"],
                           time_from="18:00", time_to="18:59")
        worker = GrabWorker(task(flt), Mock())
        worker._do_order = Mock(return_value=False)
        trains = [
            train("G1", "05:19"), train("G1", "18:00"),
            train("G2", "18:59"), train("G3", "18:30"),
            train("G2", "19:00"), train("G1", "18:45", can_buy=False),
        ]
        try:
            self.assertFalse(worker._try_grab(trains))
            self.assertEqual(worker._do_order.call_args_list,
                             [unittest.mock.call(trains[1], "二等座")])
        finally:
            worker.deleteLater()

    def test_shared_train_filter_does_not_break_no_seat_fallback(self):
        flt = TicketFilter(seat_priority=["二等座"],
                           time_from="18:00", time_to="18:59")
        worker = GrabWorker(task(flt, accept_no_seat=True), Mock())
        worker._do_order = Mock(return_value=True)
        candidate = train("G1", "18:30", {"二等座": "无", "无座": "有"})
        try:
            self.assertTrue(worker._try_grab([candidate]))
            worker._do_order.assert_called_once_with(candidate, "无座")
        finally:
            worker.deleteLater()


class TicketFilterUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        stations = StationTable(Mock())
        stations._fill({"出发站": {"code": "AAA"}, "到达站": {"code": "BBB"}})
        self.page = TaskPage({"stations": stations, "session": Mock(), "auth": Mock()})
        self.page.from_edit.set_station("出发站")
        self.page.to_edit.set_station("到达站")

    def tearDown(self):
        for worker in self.page.findChildren(QueryWorker):
            worker.wait(1000)
        self.page.deleteLater()
        self.app.processEvents()

    def set_window(self, start, end):
        self.page.time_to.setTime(QTime.fromString(end, "HH:mm"))
        self.page.time_from.setTime(QTime.fromString(start, "HH:mm"))

    def visible_codes(self):
        return [self.page.train_list.item(i).data(Qt.ItemDataRole.UserRole)
                for i in range(self.page.train_list.count())]

    def wait_for(self, condition, timeout=3):
        deadline = time.monotonic() + timeout
        while not condition() and time.monotonic() < deadline:
            self.app.processEvents()
            QTest.qWait(5)
        self.app.processEvents()
        self.assertTrue(condition(), "Qt query result delivery timed out")

    def test_query_list_filters_window_without_hiding_sold_out_trains(self):
        self.set_window("18:00", "18:59")
        trains = [train("G1", "05:19"), train("G2", "18:00"),
                  train("G3", "18:59", {"二等座": "无"}), train("G4", "19:00")]
        self.page._fill_trains(trains)
        self.assertEqual(self.visible_codes(), ["G2", "G3"])
        self.assertEqual(self.page._trains, trains)
        self.assertIn("显示 2/4 条", self.page.train_hint.text())
        self.assertEqual(self.page.time_from.displayFormat(), "HH:mm")

    def test_changing_window_refilters_cached_results_without_network(self):
        trains = [train("G1", "05:19"), train("G2", "18:00"), train("G3", "19:00")]
        self.page._fill_trains(trains)
        self.page.train_list.item(1).setCheckState(Qt.CheckState.Checked)
        with patch("ui.task_page.query_tickets") as query:
            self.set_window("18:00", "18:59")
            self.assertEqual(self.visible_codes(), ["G2"])
            self.assertEqual(self.page.train_list.item(0).checkState(),
                             Qt.CheckState.Checked)
            self.page.time_to.setTime(QTime(19, 0))
            self.assertEqual(self.visible_codes(), ["G2", "G3"])
            self.assertEqual(self.page.train_list.item(0).checkState(),
                             Qt.CheckState.Checked)
            self.assertEqual(self.page.train_list.item(1).checkState(),
                             Qt.CheckState.Unchecked)
            query.assert_not_called()

    def test_empty_window_shows_hint_without_fake_selectable_row(self):
        self.set_window("18:00", "18:59")
        self.page._fill_trains([train("G1", "05:19")])
        self.assertEqual(self.visible_codes(), [])
        self.assertIn("暂无符合条件的车次", self.page.train_hint.text())
        self.page.time_from.setTime(QTime(0, 0))
        self.assertEqual(self.visible_codes(), ["G1"])

    def test_invalid_window_blocks_query_and_start(self):
        self.set_window("19:00", "18:59")
        started = []
        self.page.sig_start_task.connect(started.append)
        with patch("ui.task_page.QueryWorker") as worker, \
                patch("ui.task_page.QMessageBox.warning") as warning:
            self.page._do_query()
            self.page._start_task()
            worker.assert_not_called()
            self.assertEqual(warning.call_count, 2)
            self.assertEqual(started, [])
        self.assertIn("时间窗无效", self.page.train_hint.text())

    def test_pending_query_uses_current_window_when_result_arrives(self):
        entered_query = threading.Event()
        release_query = threading.Event()

        def query(*args):
            entered_query.set()
            if not release_query.wait(3):
                raise RuntimeError("synthetic query timeout")
            return [train("G1", "05:19"), train("G2", "18:30")]

        try:
            with patch("ui.task_page.query_tickets", side_effect=query) as mocked:
                self.page._do_query()
                self.wait_for(entered_query.is_set)
                self.set_window("18:00", "18:59")
                release_query.set()
                self.wait_for(lambda: self.page._query_loaded
                              and self.page.query_btn.isEnabled())
                self.assertEqual(self.visible_codes(), ["G2"])
                mocked.assert_called_once()
        finally:
            release_query.set()

    def test_new_query_resets_old_train_selection(self):
        self.page._fill_trains([train("G1", "18:30")])
        self.page.train_list.item(0).setCheckState(Qt.CheckState.Checked)
        self.page._fill_trains([train("G1", "18:30")])
        self.assertEqual(self.page.train_list.item(0).checkState(),
                         Qt.CheckState.Unchecked)

    def test_task_inherits_current_time_window_and_selected_train(self):
        self.set_window("18:00", "18:59")
        self.page._fill_trains([train("G1", "05:19"), train("G2", "18:30")])
        self.page.train_list.item(0).setCheckState(Qt.CheckState.Checked)
        self.page._fill_passengers([
            Passenger("Test passenger", "1", "0000000000001234", "", "1"),
        ])
        self.page.pass_layout.itemAt(0).widget().setChecked(True)
        started = []
        self.page.sig_start_task.connect(started.append)
        self.page._start_task()
        self.assertEqual(len(started), 1)
        self.assertEqual(started[0].filter.time_from, "18:00")
        self.assertEqual(started[0].filter.time_to, "18:59")
        self.assertEqual(started[0].filter.train_codes, ["G2"])

    def test_monitor_filters_departure_time_but_still_shows_sold_out_trains(self):
        flt = TicketFilter(train_codes=["G1", "G2"], seat_priority=["二等座"],
                           time_from="18:00", time_to="18:59")
        worker = GrabWorker(task(flt), Mock())
        card = TaskCard(worker, Mock())
        try:
            card.update_tickets([
                train("G1", "05:19"), train("G1", "18:00"),
                train("G2", "18:59", {"二等座": "无"}),
                train("G3", "18:30"), train("G2", "19:00"),
            ])
            self.assertEqual(card.ticket_label.text().splitlines(),
                             ["G1 18:00  二等座:有", "G2 18:59  "])
            card.update_tickets([train("G1", "05:19")])
            self.assertEqual(card.ticket_label.text(), "暂无符合条件的车次")
        finally:
            card.deleteLater()
            worker.deleteLater()


if __name__ == "__main__":
    unittest.main()
