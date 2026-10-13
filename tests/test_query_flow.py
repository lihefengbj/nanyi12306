import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from core.query import _parse_train, _query_endpoint, query_tickets
from core.session import BASE_URL, Session


def raw_train(start_date="20261015"):
    fields = [""] * 56
    for index, value in {
        0: "synthetic%2Bticket%3D", 2: "train-K5233", 3: "K5233",
        6: "AAA", 7: "BBB", 8: "19:20", 9: "20:40", 10: "01:20",
        11: "Y", 13: start_date, 26: "有", 29: "无", 30: "--",
        53: "bed-metadata", 54: "discount-metadata",
    }.items():
        fields[index] = value
    return "|".join(fields)


def success():
    return {"status": True,
            "data": {"result": [raw_train()], "map": {"AAA": "出发站", "BBB": "到达站"}}}


def session():
    with patch.object(Session, "load_cookies"):
        result = Session()
    result.get = Mock(return_value=SimpleNamespace(
        status_code=200, url=BASE_URL + "/otn/leftTicket/init",
        text="var CLeftTicketUrl = 'leftTicket/queryG';",
    ))
    result.get_json = Mock(return_value=success())
    return result


class QueryFlowTests(unittest.TestCase):
    def test_boarding_date_is_not_train_origin_date(self):
        ticket = _parse_train(raw_train(), {}, "2026-10-16")
        self.assertEqual(ticket.date, "2026-10-16")
        self.assertEqual(ticket.start_train_date, "20261015")
        self.assertEqual(ticket.bed_level_info, "bed-metadata")
        self.assertEqual(ticket.seat_discount_info, "discount-metadata")
        self.assertEqual(ticket.seats["二等座"], "")
        self.assertEqual(ticket.secret, "synthetic+ticket=")
        self.assertEqual((ticket.from_code, ticket.to_code), ("AAA", "BBB"))

    def test_read_advertised_query_route_even_when_cookie_already_exists(self):
        s = session()
        for path in ("/passport", "/otn"):
            s._http.cookies.set("JSESSIONID", "synthetic", domain="kyfw.12306.cn", path=path)
        with patch("core.session.time.sleep"):
            tickets = query_tickets(s, "2026-10-16", "AAA", "BBB")
        s.get.assert_called_once()
        url = s.get_json.call_args.args[0]
        self.assertEqual(url, BASE_URL + "/otn/leftTicket/queryG")
        self.assertEqual(tickets[0].date, "2026-10-16")
        self.assertEqual(s.get_json.call_args.kwargs["retries"], 1)

    def test_server_route_update_is_bounded_and_same_origin(self):
        s = session()
        s.get_json.side_effect = [
            {"status": False, "c_url": "leftTicket/queryZ"}, success(),
        ]
        with patch("core.session.time.sleep"):
            query_tickets(s, "2026-10-16", "AAA", "BBB")
        self.assertEqual([c.args[0] for c in s.get_json.call_args_list],
                         [BASE_URL + "/otn/leftTicket/queryG",
                          BASE_URL + "/otn/leftTicket/queryZ"])

    def test_cached_route_reused_and_failures_force_delayed_reinitialization(self):
        s = session()
        with patch("core.session.time.sleep"):
            query_tickets(s, "2026-10-16", "AAA", "BBB")
            query_tickets(s, "2026-10-16", "AAA", "BBB")
            s.get.assert_called_once()
            s.get_json.side_effect = ValueError("synthetic invalid JSON")
            with self.assertRaises(ValueError):
                query_tickets(s, "2026-10-16", "AAA", "BBB")
            self.assertIsNone(s._query_url)
            self.assertEqual(s._query_failures, 1)

    def test_missing_or_redirected_query_page_never_guesses_route(self):
        for page in (
            SimpleNamespace(status_code=200, url=BASE_URL + "/otn/leftTicket/init",
                            text="<html>No route configuration</html>"),
            SimpleNamespace(status_code=200,
                            url="https://www.12306.cn/mormhweb/logFiles/error.html",
                            text="var CLeftTicketUrl='leftTicket/queryG'"),
        ):
            with self.subTest(page=page):
                s = session()
                s.get.return_value = page
                with self.assertRaises(RuntimeError):
                    query_tickets(s, "2026-10-16", "AAA", "BBB")
                s.get_json.assert_not_called()

    def test_reject_untrusted_query_routes(self):
        for route in (
            "https://evil.example/leftTicket/queryG",
            "//evil.example/leftTicket/queryG",
            "leftTicket/../queryG", "leftTicket/queryG?ticket=private",
            "leftTicket/queryG#fragment", "/otn/confirmPassenger/initDc",
        ):
            with self.subTest(route=route), self.assertRaises(RuntimeError):
                _query_endpoint(route)
        self.assertEqual(_query_endpoint("/otn/leftTicket/queryG"),
                         BASE_URL + "/otn/leftTicket/queryG")

    def test_shared_query_cooldown_escalates_and_success_resets_failures(self):
        s = session()
        with patch("core.session.time.monotonic", return_value=100):
            s.wait_for_query()
            self.assertEqual(s._query_not_before, 101.5)
            s.query_failed()
            self.assertEqual(s._query_not_before, 103)
            s.query_failed()
            self.assertEqual(s._query_not_before, 106)
            s.query_succeeded()
            self.assertEqual(s._query_failures, 0)
        with patch("core.session.time.monotonic", return_value=100), \
                patch("core.session.time.sleep") as sleep:
            s.wait_for_query()
            sleep.assert_called_once_with(6)

    def test_malformed_response_is_not_reported_as_successful_empty_result(self):
        for response in ([], {"status": "true"}, {"status": True, "data": {}},
                         {"status": True, "data": {"result": ["bad"], "map": []}}):
            s = session()
            s.get_json.return_value = response
            with self.subTest(response=response), self.assertRaises(RuntimeError):
                query_tickets(s, "2026-10-16", "AAA", "BBB")


if __name__ == "__main__":
    unittest.main()
