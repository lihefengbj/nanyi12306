import json
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.parse import parse_qs

import requests

from core.auth import Passenger
from core.order import (
    AuthExpiredError, ManualActionError, NoTicketError, OrderError, QueueResult,
    _checked_response, _js_object, _wait_queue, submit_candidate, submit_order,
    _safe_rejection_reason,
)
from core.query import TrainTicket
from core.session import BASE_URL, Session
from core.worker import GrabTask, GrabWorker


def ticket(seats=None):
    return TrainTicket(
        "synthetic-query-ticket", "train-K5233", "K5233", "出发站", "到达站",
        "19:20", "20:40", "01:20", "2026-10-16", True,
        {"硬座": "无", "无座": "有"} if seats is None else seats,
        start_train_date="20261015", bed_level_info="bed", seat_discount_info="discount",
    )


def passenger():
    return Passenger("Test passenger", "1", "0000000000001234", "", "1", "contact-uuid")


def log_callback():
    # A task callback is callable, not a Logger. Reject attribute access that
    # a bare Mock would silently accept (e.g. callback.debug()).
    return Mock(spec=lambda message: None)


def order_session():
    with patch.object(Session, "load_cookies"):
        s = Session()
    form = {
        "key_check_isChange": "synthetic-key",
        "leftTicketStr": "synthetic-context-ticket", "train_location": "location",
        "purpose_codes": "ADULT", "isAsync": "1",
        "queryLeftTicketRequestDTO": {"ypInfoDetail": "synthetic-yp"},
    }
    request = {
        "train_date": {"time": 1792080000000},
        "train_no": "train-K5233", "station_train_code": "K5233",
        "from_station_telecode": "AAA", "to_station_telecode": "BBB",
    }
    html = (
        "var globalRepeatSubmitToken = 'synthetic-repeat-token';"
        f"var ticketInfoForPassengerForm = {json.dumps(form)};"
        f"var orderRequestDTO = {json.dumps(request)};"
    )
    s.post = Mock(return_value=SimpleNamespace(
        status_code=200, url=BASE_URL + "/otn/confirmPassenger/initDc", text=html,
        content=html.encode("utf-8"), encoding="utf-8",
        headers={"Content-Type": "text/html;charset=UTF-8"},
    ))
    responses = {
        "checkUser": {"status": True, "data": {"flag": True}},
        "submitOrderRequest": {"status": True, "data": "0"},
        "getPassengerDTOs": {"status": True, "data": {"normal_passengers": [{
            "passenger_id_type_code": "1", "passenger_id_no": passenger().id_no,
            "allEncStr": "order-passenger-encryption",
        }]}},
        "checkOrderInfo": {"status": True, "data": {
            "submitStatus": True, "ifShowPassCode": "N", "isCheckOrderInfo": "Y",
        }},
        "getQueueCount": {"status": True, "data": {"op_2": "false"}},
        "confirmSingleForQueue": {"status": True, "data": {"submitStatus": True}},
    }
    s.responses = responses
    s.post_json = Mock(side_effect=lambda url, **kwargs: responses[url.rsplit("/", 1)[-1]])
    s.get_json = Mock(return_value={
        "status": True, "data": {"orderId": "synthetic-order-id", "waitTime": -1},
    })
    return s


class OrderFlowTests(unittest.TestCase):
    def test_flow_uses_boarding_date_and_order_context_not_query_token(self):
        s = order_session()
        messages = []
        with self.assertLogs("ny12306.order", level="DEBUG") as captured:
            result = submit_order(s, ticket(), "无座", [passenger()], messages.append)
        self.assertIs(result.result, QueueResult.SUCCESS)
        self.assertTrue(any("提交订单" in message for message in messages))
        self.assertIn("订单已提交，进入排队队列", messages)
        calls = s.post_json.call_args_list
        self.assertEqual([c.args[0].rsplit("/", 1)[-1] for c in calls], [
            "checkUser", "submitOrderRequest", "getPassengerDTOs",
            "checkOrderInfo", "getQueueCount", "confirmSingleForQueue",
        ])
        submitted = calls[1].kwargs["data"]
        self.assertEqual(submitted["secretStr"], ticket().secret)
        self.assertNotIn("secret", submitted)
        self.assertEqual(submitted["train_date"], "2026-10-16")
        self.assertEqual(submitted["back_train_date"], "2026-10-16")
        self.assertEqual(submitted["bed_level_info"], "bed")
        confirmed = calls[-1].kwargs["data"]
        self.assertEqual(confirmed["leftTicketStr"], "synthetic-context-ticket")
        self.assertEqual(confirmed["train_location"], "location")
        self.assertTrue(confirmed["passengerTicketStr"].startswith("1,0,"))
        self.assertTrue(confirmed["passengerTicketStr"].endswith("order-passenger-encryption"))
        self.assertNotIn("contact-uuid", confirmed["passengerTicketStr"])
        self.assertEqual(s.get_json.call_args.kwargs["params"]["REPEAT_SUBMIT_TOKEN"],
                         "synthetic-repeat-token")
        self.assertTrue(s.order_pending)
        for call in calls:
            self.assertEqual(call.kwargs["retries"], 1)
        text = "\n".join(captured.output)
        for private in ("synthetic-query-ticket", "synthetic-context-ticket",
                        "synthetic-repeat-token", "order-passenger-encryption",
                        passenger().id_no, passenger().name):
            self.assertNotIn(private, text)

    def test_real_worker_log_callback_completes_order_flow(self):
        s = order_session()
        worker = GrabWorker(GrabTask(
            task_id="test", train_date="2026-10-16",
            from_code="AAA", from_name="出发站", to_code="BBB", to_name="到达站",
            passengers=[passenger()],
        ), s)
        statuses, messages, successes = [], [], []
        worker.sig_status.connect(statuses.append)
        worker.sig_log.connect(messages.append)
        worker.sig_success.connect(lambda order_id, summary: successes.append(order_id))
        try:
            self.assertTrue(worker._do_order(ticket(), "无座"))
            self.assertEqual(statuses, ["下单中", "排队中", "已下单"])
            self.assertEqual(successes, ["synthetic-order-id"])
            self.assertTrue(any("提交订单 K5233 无座 x1" in message for message in messages))
            self.assertEqual(s.post_json.call_count, 6)
            s.get_json.assert_called_once()
        finally:
            worker.deleteLater()

    def test_actual_http_form_matches_official_submission_field_and_encoding(self):
        # Exercise requests' real form encoding, not a mocked post_json method.
        # Only the transport is replaced; no live account/network is involved.
        with patch.object(Session, "load_cookies"):
            s = Session()
        selected_ticket = ticket()
        selected_ticket.secret = "SYNTHETIC+/%=& encoded token"
        sent = []

        def respond(request, **kwargs):
            sent.append(request)
            if request.url.endswith("/checkUser"):
                body = {"status": True, "data": {"flag": True}}
            else:
                body = {"status": False, "messages": ["提交失败，请重试..."]}
            response = requests.Response()
            response.status_code = 200
            response.url = request.url
            response.request = request
            response.headers["Content-Type"] = "application/json"
            response._content = json.dumps(body).encode("utf-8")
            response.encoding = "utf-8"
            return response

        with patch.object(s._http, "send", side_effect=respond):
            with self.assertRaises(OrderError):
                submit_order(s, selected_ticket, "硬座", [passenger()], log_callback())
        self.assertEqual(len(sent), 2)
        submitted = sent[1]
        self.assertEqual(submitted.method, "POST")
        self.assertTrue(submitted.url.endswith("/leftTicket/submitOrderRequest"))
        fields = parse_qs(submitted.body, keep_blank_values=True)
        self.assertEqual(fields["secretStr"], [selected_ticket.secret])
        self.assertNotIn("secret", fields)
        self.assertEqual(fields["train_date"], ["2026-10-16"])
        self.assertEqual(fields["back_train_date"], ["2026-10-16"])
        self.assertTrue(submitted.headers["Content-Type"].startswith(
            "application/x-www-form-urlencoded"))

    def test_init_dc_uses_document_navigation_and_carries_form_context(self):
        s = order_session()
        s.responses["checkUser"]["attributes"] = "LOGIN_CONTEXT_PRIVATE"
        s.responses["submitOrderRequest"]["attributes"] = "ORDER_CONTEXT_PRIVATE"
        s.responses["submitOrderRequest"]["repeatSubmitToken"] = "PRE_INIT_TOKEN_PRIVATE"
        with self.assertLogs("ny12306.order", level="DEBUG") as captured:
            submit_order(s, ticket(), "硬座", [passenger()], log_callback())
        submitted = s.post_json.call_args_list[1].kwargs["data"]
        self.assertEqual(submitted["_json_att"], "LOGIN_CONTEXT_PRIVATE")
        init = s.post.call_args
        self.assertEqual(init.args[0], BASE_URL + "/otn/confirmPassenger/initDc?N")
        self.assertEqual(init.kwargs["data"], {
            "_json_att": "ORDER_CONTEXT_PRIVATE",
            "REPEAT_SUBMIT_TOKEN": "PRE_INIT_TOKEN_PRIVATE",
        })
        # Exercise the real session's header merge: None must remove default AJAX header.
        prepared = s._http.prepare_request(requests.Request(
            "POST", init.args[0], headers=init.kwargs["headers"], data=init.kwargs["data"],
        ))
        self.assertNotIn("X-Requested-With", prepared.headers)
        self.assertTrue(prepared.headers["Accept"].startswith("text/html"))
        self.assertEqual(prepared.headers["Origin"], BASE_URL)
        self.assertEqual(init.kwargs["retries"], 1)
        for call in s.post_json.call_args_list[2:]:
            self.assertEqual(call.kwargs["data"]["_json_att"], "ORDER_CONTEXT_PRIVATE")
            self.assertEqual(call.kwargs["data"]["REPEAT_SUBMIT_TOKEN"],
                             "synthetic-repeat-token")
        diagnostics = "\n".join(captured.output)
        for value in ("LOGIN_CONTEXT_PRIVATE", "ORDER_CONTEXT_PRIVATE", "PRE_INIT_TOKEN_PRIVATE"):
            self.assertNotIn(value, diagnostics)

    def test_init_dc_route_flag_matches_station_pair(self):
        for from_code, to_code, expected in (
            ("AAA", "BBB", "N"), ("VCM", "BBB", "Y"), ("AAA", "VOM", "Y"),
        ):
            s = order_session()
            selected = ticket()
            selected.from_code, selected.to_code = from_code, to_code
            with self.subTest(from_code=from_code, to_code=to_code):
                submit_order(s, selected, "硬座", [passenger()], log_callback())
                self.assertEqual(s.post.call_args.args[0],
                                 BASE_URL + f"/otn/confirmPassenger/initDc?{expected}")

    def test_order_forms_use_latest_server_context_through_queue_polling(self):
        s = order_session()
        s.responses["getPassengerDTOs"]["attributes"] = "PASSENGER_CONTEXT_PRIVATE"
        s.responses["checkOrderInfo"]["repeatSubmitToken"] = "CHECK_CONTEXT_TOKEN_PRIVATE"
        s.responses["getQueueCount"]["attributes"] = "QUEUE_CONTEXT_PRIVATE"
        submit_order(s, ticket(), "硬座", [passenger()], log_callback())
        check_form = s.post_json.call_args_list[3].kwargs["data"]
        self.assertEqual(check_form["_json_att"], "PASSENGER_CONTEXT_PRIVATE")
        queue_form = s.post_json.call_args_list[4].kwargs["data"]
        self.assertEqual(queue_form["REPEAT_SUBMIT_TOKEN"], "CHECK_CONTEXT_TOKEN_PRIVATE")
        confirm_form = s.post_json.call_args_list[5].kwargs["data"]
        self.assertEqual(confirm_form["_json_att"], "QUEUE_CONTEXT_PRIVATE")
        self.assertEqual(confirm_form["REPEAT_SUBMIT_TOKEN"], "CHECK_CONTEXT_TOKEN_PRIVATE")
        wait_form = s.get_json.call_args.kwargs["params"]
        self.assertEqual(wait_form["_json_att"], "QUEUE_CONTEXT_PRIVATE")
        self.assertEqual(wait_form["REPEAT_SUBMIT_TOKEN"], "CHECK_CONTEXT_TOKEN_PRIVATE")

    def test_init_dc_error_redirect_is_not_accepted_or_followed_by_confirmation(self):
        s = order_session()
        s.post.return_value.url = (
            "https://www.12306.cn/mormhweb/logFiles/error.html?private=URL_PRIVATE")
        with self.assertLogs("ny12306.order", level="DEBUG") as captured:
            with self.assertRaisesRegex(OrderError, "未返回本站订单页") as raised:
                submit_order(s, ticket(), "硬座", [passenger()], log_callback())
        self.assertEqual(s.post_json.call_count, 2)
        self.assertIn("/mormhweb/logFiles/error.html", str(raised.exception))
        self.assertNotIn("URL_PRIVATE", str(raised.exception) + "\n".join(captured.output))
        self.assertFalse(s.order_pending)

    def test_init_dc_requests_real_document_headers_at_transport_boundary(self):
        s = order_session()
        initial_html = s.post.return_value.text
        s.post = Session.post.__get__(s, Session)
        s.post_json = Session.post_json.__get__(s, Session)
        s.get_json = Session.get_json.__get__(s, Session)
        sent = []

        def respond(request, **kwargs):
            sent.append(request)
            response = requests.Response()
            response.status_code = 200
            response.url = request.url
            response.request = request
            response.encoding = "utf-8"
            path = request.url.split("?", 1)[0].rsplit("/", 1)[-1]
            if path == "initDc":
                response.headers["Content-Type"] = "text/html;charset=UTF-8"
                response._content = initial_html.encode("utf-8")
            else:
                body = ({"status": True, "data": {"orderId": "synthetic-order-id"}}
                        if path == "queryOrderWaitTime" else s.responses[path])
                response.headers["Content-Type"] = "application/json;charset=UTF-8"
                response._content = json.dumps(body).encode("utf-8")
            return response

        with patch.object(s._http, "send", side_effect=respond):
            result = submit_order(s, ticket(), "硬座", [passenger()], log_callback())
        self.assertIs(result.result, QueueResult.SUCCESS)
        navigation = next(request for request in sent if "/initDc?" in request.url)
        self.assertEqual(navigation.method, "POST")
        self.assertNotIn("X-Requested-With", navigation.headers)
        self.assertTrue(navigation.headers["Accept"].startswith("text/html"))
        self.assertEqual(parse_qs(navigation.body, keep_blank_values=True), {"_json_att": [""]})
        self.assertEqual(len(sent), 8)

    def test_rejection_diagnostics_keep_fixed_reason_without_private_server_text(self):
        data = {"status": False, "messages": [
            f"提交失败，请重试... {passenger().name} {passenger().id_no} PRIVATE_TICKET",
        ]}
        with self.assertLogs("ny12306.order", level="WARNING") as captured:
            with self.assertRaises(OrderError) as raised:
                _checked_response(data, "submitOrderRequest")
        diagnostic = "\n".join(captured.output) + str(raised.exception)
        self.assertIn("提交失败，请重试", diagnostic)
        self.assertIn("服务端未说明具体原因", diagnostic)
        self.assertNotIn(passenger().name, diagnostic)
        self.assertNotIn(passenger().id_no, diagnostic)
        self.assertNotIn("PRIVATE_TICKET", diagnostic)
        self.assertEqual(_safe_rejection_reason({
            "messages": ["UNKNOWN PRIVATE_TICKET"],
        }), "服务端拒绝请求，原因未识别（消息长度=22，正文未记录）")
        self.assertEqual(_safe_rejection_reason({
            "data": {"errMsg": "距开车时间太近，已停止售票 PRIVATE_TICKET"},
        }), "距发车太近或已停止售票")
        self.assertEqual(_safe_rejection_reason({
            "data": {"errMsg": "与已购车票行程冲突 PRIVATE_TICKET"},
        }), "与已有订单行程冲突，请在 12306 App 查看")

    def test_failed_login_prevents_order_submission(self):
        s = order_session()
        s.responses["checkUser"]["data"]["flag"] = False
        with self.assertRaises(AuthExpiredError):
            submit_order(s, ticket(), "无座", [passenger()], log_callback())
        self.assertEqual(s.post_json.call_count, 1)
        s.post.assert_not_called()

    def test_generic_submit_failure_is_not_false_login_expiry_or_no_ticket(self):
        s = order_session()
        s.responses["submitOrderRequest"] = {
            "status": False, "data": {}, "messages": ["提交失败，请重试..."],
        }
        with self.assertRaises(OrderError) as raised:
            submit_order(s, ticket(), "无座", [passenger()], log_callback())
        self.assertIs(type(raised.exception), OrderError)
        self.assertIn("submitOrderRequest", str(raised.exception))
        s.post.assert_not_called()
        self.assertEqual(s._order_failures, 1)

    def test_explicit_no_ticket_is_classified_before_generic_failure(self):
        with self.assertRaises(NoTicketError):
            _checked_response({"status": False, "messages": ["没有可用的票"]},
                              "submitOrderRequest")

    def test_missing_submit_status_never_claims_success(self):
        for payload in ({}, {"submitStatus": "true"}, {"submitStatus": False}):
            with self.subTest(payload=payload), self.assertRaises(OrderError):
                _checked_response({"status": True, "data": payload}, "checkOrderInfo", True)

    def test_captcha_stops_before_confirmation_and_requires_app(self):
        s = order_session()
        s.responses["checkOrderInfo"]["data"]["ifShowPassCode"] = "Y"
        with self.assertRaises(ManualActionError):
            submit_order(s, ticket(), "无座", [passenger()], log_callback())
        self.assertTrue(s.order_pending)
        self.assertNotIn("confirmSingleForQueue",
                         [c.args[0].rsplit("/", 1)[-1] for c in s.post_json.call_args_list])

    def test_missing_order_passenger_encryption_is_not_replaced_by_contact_uuid(self):
        s = order_session()
        s.responses["getPassengerDTOs"]["data"]["normal_passengers"][0].pop("allEncStr")
        with self.assertRaisesRegex(OrderError, "本轮订单加密标识"):
            submit_order(s, ticket(), "无座", [passenger()], log_callback())
        self.assertEqual(s.post_json.call_count, 3)

    def test_wrong_order_context_date_stops_before_passenger_confirmation(self):
        s = order_session()
        s.post.return_value.text = s.post.return_value.text.replace(
            "1792080000000", "1791993600000")
        with self.assertRaisesRegex(OrderError, "日期与所选日期不一致"):
            submit_order(s, ticket(), "无座", [passenger()], log_callback())
        self.assertEqual(s.post_json.call_count, 2)

    def test_confirmation_response_loss_never_replays_submission(self):
        s = order_session()
        def post(url, **kwargs):
            step = url.rsplit("/", 1)[-1]
            if step == "confirmSingleForQueue":
                raise TimeoutError("synthetic response loss")
            return s.responses[step]
        s.post_json.side_effect = post
        with self.assertRaises(ManualActionError):
            submit_order(s, ticket(), "无座", [passenger()], log_callback())
        self.assertTrue(s.order_pending)
        count = s.post_json.call_count
        with self.assertRaises(ManualActionError):
            submit_order(s, ticket(), "无座", [passenger()], log_callback())
        self.assertEqual(s.post_json.call_count, count)

    def test_unclear_confirmation_response_keeps_pending_guard(self):
        s = order_session()
        s.responses["confirmSingleForQueue"]["data"] = {}
        with self.assertRaises(ManualActionError):
            submit_order(s, ticket(), "无座", [passenger()], log_callback())
        self.assertTrue(s.order_pending)

    def test_explicit_confirmation_rejection_can_retry_after_cooldown(self):
        s = order_session()
        s.responses["confirmSingleForQueue"]["data"] = {"submitStatus": False}
        with self.assertRaises(OrderError):
            submit_order(s, ticket(), "无座", [passenger()], log_callback())
        self.assertFalse(s.order_pending)
        self.assertGreater(s._order_not_before, 0)

    def test_duplicate_submission_hint_stops_retrying(self):
        s = order_session()
        s.responses["confirmSingleForQueue"]["data"] = {
            "submitStatus": False, "errMsg": "请勿重复提交订单",
        }
        with self.assertRaises(ManualActionError) as raised:
            submit_order(s, ticket(), "无座", [passenger()], log_callback())
        self.assertIn("请勿重复提交", str(raised.exception))
        # The pending guard stays on: a duplicate hint means an order may exist.
        self.assertTrue(s.order_pending)

    def test_queue_timeout_is_unknown_not_safe_to_resubmit(self):
        s = order_session()
        s.order_pending = True
        with self.assertRaises(ManualActionError):
            _wait_queue(s, log_callback(), timeout=0)
        self.assertTrue(s.order_pending)
        s.get_json.assert_not_called()

    def test_pending_order_blocks_other_task_before_any_requests(self):
        s = order_session()
        s.order_pending = True
        with self.assertRaises(ManualActionError):
            submit_order(s, ticket(), "无座", [passenger()], log_callback())
        s.post_json.assert_not_called()

    def test_shared_order_failure_cooldown(self):
        s = order_session()
        s.responses["submitOrderRequest"]["status"] = False
        with patch("core.order.time.monotonic", return_value=100):
            with self.assertRaises(OrderError):
                submit_order(s, ticket(), "无座", [passenger()], log_callback())
            self.assertEqual(s._order_not_before, 105)
            count = s.post_json.call_count
            with self.assertRaisesRegex(OrderError, "冷却期"):
                submit_order(s, ticket(), "无座", [passenger()], log_callback())
            self.assertEqual(s.post_json.call_count, count)
        with patch("core.order.time.monotonic", return_value=106):
            with self.assertRaises(OrderError):
                submit_order(s, ticket(), "无座", [passenger()], log_callback())
            self.assertEqual(s._order_not_before, 116)

    def test_order_context_parser_does_not_execute_javascript(self):
        self.assertEqual(_js_object("var x = {'nested': {'value':'a}b'}, 'ok':true};", "x"),
                         {"nested": {"value": "a}b"}, "ok": True})
        with self.assertRaises(OrderError):
            _js_object("var x = {'value': dangerousFunction()};", "x")

    def test_order_page_accepts_javascript_literals_not_only_json(self):
        s = order_session()
        html = r"""
        <script>
        var globalRepeatSubmitToken = 'synthetic-repeat-token';
        var ticketInfoForPassengerForm = {
            key_check_isChange: 'synthetic-key',
            leftTicketStr: 'synthetic\x2Bcontext\u002Fticket\x3D',
            train_location: 'location',
            purpose_codes: 'ADULT',
            isAsync: '1',
            queryLeftTicketRequestDTO: {
                ypInfoDetail: 'synthetic-yp', // object contains trailing commas
            },
        };
        var orderRequestDTO = {
            train_date: {time: 1792080000000,},
            train_no: 'train-K5233',
            station_train_code: 'K5233',
            from_station_telecode: 'AAA',
            to_station_telecode: 'BBB',
        };
        </script>
        """
        s.post.return_value.text = html
        s.post.return_value.content = html.encode("utf-8")
        result = submit_order(s, ticket(), "硬座", [passenger()], log_callback())
        self.assertIs(result.result, QueueResult.SUCCESS)
        confirmed = s.post_json.call_args_list[-1].kwargs["data"]
        self.assertEqual(confirmed["leftTicketStr"], "synthetic+context/ticket=")
        self.assertEqual(s.post_json.call_count, 6)

    def test_unsupported_order_object_stops_before_confirmation_with_safe_offset(self):
        s = order_session()
        s.post.return_value.text = (
            "var globalRepeatSubmitToken = 'PRIVATE_TICKET';"
            "var ticketInfoForPassengerForm = {key_check_isChange: 'PRIVATE_KEY',"
            "other: dangerousFunction()};")
        with self.assertLogs("ny12306.order", level="WARNING") as captured:
            with self.assertRaisesRegex(OrderError, "unsupported_expression") as raised:
                submit_order(s, ticket(), "硬座", [passenger()], log_callback())
        self.assertEqual(s.post_json.call_count, 2)
        diagnostic = str(raised.exception) + "\n".join(captured.output)
        self.assertIn("offset=", diagnostic)
        self.assertNotIn("PRIVATE_TICKET", diagnostic)
        self.assertNotIn("PRIVATE_KEY", diagnostic)

    def test_unverified_candidate_does_not_send_any_request(self):
        s = order_session()
        with self.assertRaises(ManualActionError):
            submit_candidate(s, ticket(), "无座", [passenger()], log_callback())
        s.post_json.assert_not_called()
        s.post.assert_not_called()

    def test_concurrent_orders_cannot_interleave_or_duplicate(self):
        s = order_session()
        entered = threading.Event()
        release = threading.Event()
        events, results = [], []
        def submit(*args):
            events.append(threading.current_thread().name)
            entered.set()
            if not release.wait(3):
                raise TimeoutError("synthetic flow block")
            s.order_pending = True
            return "success"
        def run():
            try:
                results.append(submit_order(s, ticket(), "无座", [passenger()], log_callback()))
            except ManualActionError:
                results.append("blocked")
        first = threading.Thread(target=run, name="first")
        second = threading.Thread(target=run, name="second")
        with patch("core.order._submit_order", side_effect=submit):
            try:
                first.start()
                self.assertTrue(entered.wait(1))
                second.start()
                release.set()
                first.join(3)
                second.join(3)
                self.assertEqual(events, ["first"])
                self.assertCountEqual(results, ["success", "blocked"])
            finally:
                release.set()
                first.join(3)
                if second.ident is not None:
                    second.join(3)

    def test_internal_error_diagnostics_include_location_not_private_message(self):
        s = order_session()
        with patch("core.order._submit_order",
                   side_effect=AttributeError("PRIVATE_PASSENGER PRIVATE_TICKET")), \
                self.assertLogs("ny12306.order", level="ERROR") as captured:
            with self.assertRaises(OrderError) as raised:
                submit_order(s, ticket(), "无座", [passenger()], log_callback())
        diagnostic = "\n".join(captured.output) + str(raised.exception)
        self.assertIn("type=AttributeError", diagnostic)
        self.assertIn("order.py:", diagnostic)
        self.assertIn("submit_order", diagnostic)
        self.assertNotIn("PRIVATE_PASSENGER", diagnostic)
        self.assertNotIn("PRIVATE_TICKET", diagnostic)
        s.post_json.assert_not_called()


if __name__ == "__main__":
    unittest.main()
