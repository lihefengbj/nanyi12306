import logging
import unittest
from types import SimpleNamespace

import requests
from requests.cookies import RequestsCookieJar
from unittest.mock import Mock

from core.auth import Auth, QrStatus
from core.session import BASE_URL
from ui.login_page import QrWorker


class FakeSession:
    def __init__(self, response):
        self._http = SimpleNamespace(cookies={})
        self.response = response

    def post_json(self, *args, **kwargs):
        return self.response


class LoginFlowTests(unittest.TestCase):
    def test_check_qr_maps_confirmed_response(self):
        auth = Auth(FakeSession({"result_code": 2, "uamtk": "token"}))

        status, token = auth.check_qr("uuid")

        self.assertIs(status, QrStatus.CONFIRMED)
        self.assertEqual(token, "token")

    def test_check_qr_rejects_unrecognized_response(self):
        auth = Auth(FakeSession({"message": "unexpected response"}))

        with self.assertRaisesRegex(RuntimeError, "result_code=缺失"):
            auth.check_qr("uuid")

    def test_check_qr_logs_protocol_status_without_ticket_or_uuid(self):
        auth = Auth(FakeSession({
            "result_code": "0", "uamtk": "PRIVATE_SCAN_TICKET",
        }))
        auth._qr_generation = 2
        with self.assertLogs("ny12306.auth", level="DEBUG") as captured:
            status, _ = auth.check_qr("PRIVATE_SCAN_UUID")
        self.assertIs(status, QrStatus.WAITING)
        text = "\n".join(captured.output)
        self.assertIn("generation=2", text)
        self.assertIn("result_code=0", text)
        self.assertNotIn("PRIVATE_SCAN_TICKET", text)
        self.assertNotIn("PRIVATE_SCAN_UUID", text)

    def test_worker_reports_session_exchange_failure(self):
        class ConfirmedAuth:
            def create_qr(self):
                return "uuid", b"png"

            def check_qr(self, uuid):
                return QrStatus.CONFIRMED, "token"

            def finish_login(self, token):
                raise RuntimeError("session exchange failed")

        worker = QrWorker(ConfirmedAuth())
        errors = []
        worker.sig_login_error.connect(errors.append)

        with self.assertLogs("ny12306.login", level=logging.ERROR):
            worker.run()

        self.assertEqual(errors, ["session exchange failed"])

    def test_create_qr_loads_login_configuration_first(self):
        class CapturingSession:
            def __init__(self):
                self._http = SimpleNamespace(cookies=RequestsCookieJar())
                self.post_calls = []

            def get(self, url, **kwargs):
                self.page_url = url

            def post_json(self, url, **kwargs):
                self.post_calls.append((url, kwargs))
                if url.endswith("/otn/login/conf"):
                    return {
                        "data": {
                            "is_uam_login": "Y",
                            "is_sweep_login": "Y",
                        }
                    }
                return {
                    "result_code": "0",
                    "uuid": "qr-uuid",
                    "image": "cG5n",
                }

        session = CapturingSession()

        uuid, image = Auth(session).create_qr()

        self.assertEqual((uuid, image), ("qr-uuid", b"png"))
        self.assertEqual(session.page_url, f"{BASE_URL}/otn/resources/login.html")
        self.assertEqual(
            [call[0] for call in session.post_calls],
            [
                f"{BASE_URL}/otn/login/conf",
                f"{BASE_URL}/passport/web/create-qr64",
            ],
        )

    def test_create_qr_rejects_disabled_server_qr_login(self):
        class ConfigSession:
            _http = SimpleNamespace(cookies=RequestsCookieJar())

            def get(self, url, **kwargs):
                pass

            def post_json(self, url, **kwargs):
                if url.endswith("/otn/login/conf"):
                    return {"data": {"is_uam_login": "Y", "is_sweep_login": "N"}}
                raise AssertionError("QR endpoint must not be called")

        with self.assertRaisesRegex(RuntimeError, "未启用统一认证扫码登录"):
            Auth(ConfigSession()).create_qr()

    def test_finish_login_preserves_scoped_ticket_and_completes_passport_flow(self):
        class CapturingSession:
            def __init__(self, cookies):
                self._http = SimpleNamespace(cookies=cookies)
                self.get_calls = []
                self.post_calls = []
                self.events = []
                self.saved = False

            def get(self, url, **kwargs):
                self.get_calls.append((url, kwargs))
                self.events.append(("GET", url))
                return SimpleNamespace(
                    url=f"{BASE_URL}/otn/passport?redirect=/otn/login/userLogin",
                    status_code=200,
                    headers={"Content-Type": "text/html;charset=UTF-8"},
                    content=b"<!doctype html><html></html>",
                    encoding="utf-8",
                )

            def post_json(self, url, **kwargs):
                self.post_calls.append((url, kwargs))
                self.events.append(("POST", url))
                if url.endswith("/passport/web/auth/uamtk"):
                    return {"result_code": "0", "newapptk": "new-business-ticket"}
                if url.endswith("/otn/uamauthclient"):
                    return {"result_code": "0"}
                if url.endswith("/otn/login/checkUser"):
                    return {"status": True, "data": {"flag": True}}
                raise AssertionError("unexpected POST endpoint")

            def save_cookies(self):
                self.saved = True

        cookies = RequestsCookieJar()
        cookies.set("uamtk", "stale-root", domain="kyfw.12306.cn", path="/")
        cookies.set("uamtk", "stale-passport", domain="kyfw.12306.cn",
                    path="/passport")
        cookies.set("uamtk", "stale-other-host", domain="passport.12306.cn",
                    path="/passport")
        # checkqr has already installed the current host/path ticket.
        current = requests.cookies.create_cookie(
            "uamtk", "current-scan-ticket", domain="kyfw.12306.cn",
            path="/passport", secure=True,
        )
        current.domain_specified = False
        cookies.set_cookie(current)
        session = CapturingSession(cookies)
        auth = Auth(session)
        auth._login_config = {"is_uam_login": "Y", "is_sweep_login": "Y"}

        auth.finish_login("current-scan-ticket")

        uamtk_cookies = [cookie for cookie in cookies if cookie.name == "uamtk"]
        self.assertEqual(len(uamtk_cookies), 1)
        self.assertEqual(uamtk_cookies[0].value, "current-scan-ticket")
        self.assertEqual(uamtk_cookies[0].domain, "kyfw.12306.cn")
        self.assertEqual(uamtk_cookies[0].path, "/passport")
        self.assertIs(uamtk_cookies[0], current)

        url, kwargs = session.post_calls[0]
        self.assertEqual(url, f"{BASE_URL}/passport/web/auth/uamtk")
        self.assertEqual(kwargs["data"], {"appid": "otn"})
        self.assertNotIn("callback", kwargs)
        self.assertNotIn("params", kwargs)
        self.assertEqual(
            kwargs["headers"]["Referer"],
            f"{BASE_URL}/otn/passport?redirect=/otn/login/userLogin",
        )
        self.assertNotIn("X-Requested-With", kwargs["headers"])
        http = requests.Session()
        http.headers.update({
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "X-Requested-With": "XMLHttpRequest",
        })
        http.cookies.update(cookies)
        prepared = http.prepare_request(
            requests.Request(
                "POST",
                url,
                data=kwargs["data"],
                headers=kwargs["headers"],
            )
        )
        self.assertEqual(
            prepared.headers.get("Cookie"),
            "uamtk=current-scan-ticket",
        )
        self.assertTrue(prepared.headers["Accept"].startswith("application/json"))
        self.assertEqual(
            prepared.headers["X-Requested-With"],
            "XMLHttpRequest",
        )
        self.assertNotIn("callback=", prepared.url)
        self.assertEqual(prepared.body, "appid=otn")
        self.assertEqual(
            [call[0] for call in session.post_calls],
            [
                f"{BASE_URL}/passport/web/auth/uamtk",
                f"{BASE_URL}/otn/uamauthclient",
                f"{BASE_URL}/otn/login/checkUser",
            ],
        )
        self.assertEqual(
            session.post_calls[1][1]["data"],
            {"tk": "new-business-ticket"},
        )
        self.assertEqual(session.events, [
            ("GET", f"{BASE_URL}/otn/login/userLogin"),
            ("POST", f"{BASE_URL}/passport/web/auth/uamtk"),
            ("POST", f"{BASE_URL}/otn/uamauthclient"),
            ("GET", f"{BASE_URL}/otn/login/userLogin"),
            ("POST", f"{BASE_URL}/otn/login/checkUser"),
        ])
        business_request = http.prepare_request(
            requests.Request("POST", f"{BASE_URL}/otn/login/checkUser")
        )
        self.assertNotIn("uamtk=", business_request.headers.get("Cookie", ""))
        self.assertTrue(session.saved)

    def test_exchange_failure_does_not_save_or_claim_logged_in(self):
        session = Mock()
        session._http = SimpleNamespace(cookies=RequestsCookieJar())
        session.get.return_value = SimpleNamespace(
            url=f"{BASE_URL}/otn/passport?redirect=/otn/login/userLogin",
            status_code=200, headers={"Content-Type": "text/html"},
            content=b"<html></html>", encoding="utf-8",
        )
        session.post_json.return_value = {"result_code": "91"}
        auth = Auth(session)
        auth._login_config = {"is_uam_login": "Y", "is_sweep_login": "Y"}
        with self.assertRaisesRegex(RuntimeError, "result_code=91"):
            auth.finish_login("ticket")
        session.save_cookies.assert_not_called()
        self.assertEqual(session.post_json.call_count, 1)

    def test_failed_check_user_never_saves_cookie_state(self):
        session = Mock()
        session._http = SimpleNamespace(cookies=RequestsCookieJar())
        session.get.return_value = SimpleNamespace(
            url=f"{BASE_URL}/otn/passport?redirect=/otn/login/userLogin",
            status_code=200, headers={"Content-Type": "text/html"},
            content=b"<html></html>", encoding="utf-8",
        )
        session.post_json.side_effect = [
            {"result_code": "0", "newapptk": "ticket"},
            {"result_code": "0"},
            {"status": True, "data": {"flag": False}},
        ]
        auth = Auth(session)
        auth._login_config = {"is_uam_login": "Y", "is_sweep_login": "Y"}
        with self.assertRaisesRegex(RuntimeError, "会话校验未通过"):
            auth.finish_login("ticket")
        session.save_cookies.assert_not_called()

    def test_external_error_page_never_receives_ticket_exchange(self):
        session = Mock()
        session._http = SimpleNamespace(cookies=RequestsCookieJar())
        session.get.return_value = SimpleNamespace(
            url="https://www.12306.cn/mormhweb/logFiles/error.html",
            status_code=200,
        )
        auth = Auth(session)
        auth._login_config = {"is_uam_login": "Y", "is_sweep_login": "Y"}
        with self.assertRaisesRegex(RuntimeError, "登录导航异常"):
            auth.finish_login("ticket")
        session.post_json.assert_not_called()
        session.save_cookies.assert_not_called()

    def test_auth_client_rejection_does_not_save_cookie_state(self):
        session = Mock()
        session._http = SimpleNamespace(cookies=RequestsCookieJar())
        session.get.return_value = SimpleNamespace(
            url=f"{BASE_URL}/otn/passport?redirect=/otn/login/userLogin",
            status_code=200, headers={"Content-Type": "text/html"},
            content=b"<html></html>", encoding="utf-8",
        )
        session.post_json.side_effect = [
            {"result_code": "0", "apptk": "fallback-ticket"},
            {"result_code": "1"},
        ]
        auth = Auth(session)
        auth._login_config = {"is_uam_login": "Y", "is_sweep_login": "Y"}
        with self.assertRaisesRegex(RuntimeError, "result_code=1"):
            auth.finish_login("ticket")
        self.assertEqual(
            session.post_json.call_args.kwargs["data"],
            {"tk": "fallback-ticket"},
        )
        session.save_cookies.assert_not_called()

    def test_check_user_rejects_string_false_and_unsuccessful_envelope(self):
        for response in [
            {"status": True, "data": {"flag": "false"}},
            {"status": False, "data": {"flag": True}},
            {"status": True, "data": None},
        ]:
            with self.subTest(response=response):
                self.assertFalse(Auth(FakeSession(response)).check_user())

    def test_passenger_queries_require_verified_login(self):
        auth = Auth(Mock())
        auth.check_user = Mock(return_value=False)
        auth._passengers_from_query = Mock()
        auth._passengers_from_confirm = Mock()
        with self.assertRaisesRegex(RuntimeError, "登录会话未建立"):
            auth.get_passengers()
        auth._passengers_from_query.assert_not_called()
        auth._passengers_from_confirm.assert_not_called()

    def test_contact_query_precedes_order_context_query(self):
        auth = Auth(Mock())
        auth.check_user = Mock(return_value=True)
        auth._passengers_from_query = Mock(return_value=["passenger"])
        auth._passengers_from_confirm = Mock()
        self.assertEqual(auth.get_passengers(), ["passenger"])
        auth._passengers_from_confirm.assert_not_called()

    def test_contact_query_failure_falls_back_to_order_context(self):
        auth = Auth(Mock())
        auth.check_user = Mock(return_value=True)
        auth._passengers_from_query = Mock(side_effect=RuntimeError("unavailable"))
        auth._passengers_from_confirm = Mock(return_value=["passenger"])
        self.assertEqual(auth.get_passengers(), ["passenger"])
        auth._passengers_from_confirm.assert_called_once_with(2)

    def test_check_user_logs_only_boolean_login_state(self):
        auth = Auth(FakeSession({
            "status": True,
            "data": {"flag": True, "user_name": "PRIVATE_NAME"},
        }))

        with self.assertLogs("ny12306.auth", level="DEBUG") as captured:
            self.assertTrue(auth.check_user())

        diagnostic_text = "\n".join(captured.output)
        self.assertIn("login_flag=True", diagnostic_text)
        self.assertNotIn("PRIVATE_NAME", diagnostic_text)


if __name__ == "__main__":
    unittest.main()
