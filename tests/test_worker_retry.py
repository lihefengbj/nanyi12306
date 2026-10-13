import unittest
from unittest.mock import Mock, patch

from core.order import AuthExpiredError, ManualActionError, OrderError
from core.query import TicketFilter
from core.worker import GrabTask, GrabWorker


class WorkerRetryTests(unittest.TestCase):
    def worker(self):
        return GrabWorker(GrabTask(
            "test", "2026-10-16", "AAA", "出发站", "BBB", "到达站",
            filter=TicketFilter(),
        ), Mock())

    def test_successful_queries_do_not_reset_order_failure_backoff(self):
        worker = self.worker()
        worker._try_grab = Mock(side_effect=[
            OrderError("submitOrderRequest: rejected"),
            OrderError("submitOrderRequest: rejected"),
            OrderError("submitOrderRequest: rejected"), True,
        ])
        delays, logs = [], []
        worker._sleep = delays.append
        worker.sig_log.connect(logs.append)
        try:
            with patch("core.worker.load_config", return_value={"poll_interval": 2}), \
                    patch("core.worker.query_tickets", return_value=[]):
                worker.run()
            self.assertEqual(delays, [5, 10, 20])
            self.assertEqual(worker.poll_count, 4)
            self.assertTrue(all("下单异常" in line for line in logs[1:]))
            self.assertTrue(all("轮询异常" not in line for line in logs))
        finally:
            worker.deleteLater()

    def test_query_errors_have_separate_backoff_and_never_attempt_order(self):
        worker = self.worker()
        delays = []
        def sleep(seconds):
            delays.append(seconds)
            if len(delays) == 3:
                worker.stop()
        worker._sleep = sleep
        worker._try_grab = Mock()
        try:
            with patch("core.worker.load_config", return_value={"poll_interval": 2}), \
                    patch("core.worker.query_tickets", side_effect=RuntimeError("HTML error")):
                worker.run()
            self.assertEqual(delays, [3, 6, 12])
            worker._try_grab.assert_not_called()
        finally:
            worker.deleteLater()

    def test_manual_action_or_auth_expiry_stops_without_retry(self):
        for error, expected in (
            (ManualActionError("check App"), "需人工处理"),
            (AuthExpiredError("expired"), "登录失效"),
        ):
            worker = self.worker()
            worker._try_grab = Mock(side_effect=error)
            worker._sleep = Mock()
            statuses = []
            worker.sig_status.connect(statuses.append)
            try:
                with patch("core.worker.load_config", return_value={"poll_interval": 2}), \
                        patch("core.worker.query_tickets", return_value=[]):
                    worker.run()
                self.assertEqual(statuses[-1], expected)
                worker._sleep.assert_not_called()
            finally:
                worker.deleteLater()


if __name__ == "__main__":
    unittest.main()
