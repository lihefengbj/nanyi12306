import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import requests

from core.session import JsonResponseError, Session


class FakeResponse:
    status_code = 200
    headers = {
        "Content-Type": "text/html; charset=utf-8",
        "Set-Cookie": "sessionid=COOKIE_MARKER",
    }
    url = "https://kyfw.12306.cn/otn/login/init?redirect-token=URL_MARKER"
    encoding = "utf-8"
    content = (
        b"\xef\xbb\xbf<!DOCTYPE html PUBLIC "
        b"'-//W3C//DTD XHTML 1.0 Transitional//EN'>"
        b"<html>private response body</html>"
    )
    text = content.decode("utf-8")

    def json(self):
        raise ValueError("invalid JSON")


class FakeHttp:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.response


class HttpDiagnosticsTests(unittest.TestCase):
    def test_post_timeout_is_not_transparently_replayed(self):
        session = Session.__new__(Session)
        session._http = FakeHttp(FakeResponse())
        session._lock = threading.Lock()
        with patch.object(session._http, "request", side_effect=requests.Timeout) as request, \
                patch("core.session.time.sleep") as sleep:
            with self.assertRaises(requests.Timeout):
                session.post("https://kyfw.12306.cn/otn/leftTicket/submitOrderRequest")
            self.assertEqual(request.call_count, 1)
            sleep.assert_not_called()

    def test_json_error_preserves_safe_response_metadata(self):
        session = Session.__new__(Session)
        session._http = FakeHttp(FakeResponse())
        session._lock = threading.Lock()
        request_url = (
            "https://kyfw.12306.cn/passport/web/auth/uamtk?ticket=REQUEST_MARKER"
        )

        with self.assertLogs("ny12306.http", level="DEBUG") as captured:
            with self.assertRaises(JsonResponseError) as raised:
                session.post_json(
                    request_url,
                    data={
                        "tk": "TOKEN_MARKER",
                        "passenger_id_no": "PASSENGER_ID_MARKER",
                        "passenger_name": "PASSENGER_NAME_MARKER",
                    },
                )

        error = raised.exception
        self.assertEqual(error.status_code, 200)
        self.assertEqual(error.content_type, "text/html; charset=utf-8")
        self.assertEqual(error.response_kind, "xhtml")
        self.assertEqual(
            error.request_url,
            "https://kyfw.12306.cn/passport/web/auth/uamtk",
        )
        self.assertEqual(
            error.final_url,
            "https://kyfw.12306.cn/otn/login/init",
        )

        diagnostic_text = str(error) + "\n".join(captured.output)
        for sensitive_value in (
            "REQUEST_MARKER", "URL_MARKER", "TOKEN_MARKER",
            "PASSENGER_ID_MARKER", "PASSENGER_NAME_MARKER", "COOKIE_MARKER",
            "private response body",
        ):
            self.assertNotIn(sensitive_value, diagnostic_text)
        self.assertIn("params_fields=", diagnostic_text)
        self.assertIn("data_fields=passenger_id_no,passenger_name,tk", diagnostic_text)
        self.assertIn("set_cookie=yes", diagnostic_text)

    def test_redirect_diagnostics_hide_query_values(self):
        response = FakeResponse()
        response.history = [
            SimpleNamespace(
                status_code=302,
                url="https://kyfw.12306.cn/otn/login/userLogin?token=SOURCE_SECRET",
                headers={"Location": "https://www.12306.cn/otn/passport?ticket=TARGET_SECRET"},
            )
        ]
        session = Session.__new__(Session)
        session._http = FakeHttp(response)
        session._lock = threading.Lock()

        with self.assertLogs("ny12306.http", level="DEBUG") as captured:
            session.get("https://kyfw.12306.cn/otn/login/userLogin")

        diagnostic_text = "\n".join(captured.output)
        self.assertIn(
            "redirects=302:https://kyfw.12306.cn/otn/login/userLogin"
            "->https://www.12306.cn/otn/passport",
            diagnostic_text,
        )
        self.assertNotIn("SOURCE_SECRET", diagnostic_text)
        self.assertNotIn("TARGET_SECRET", diagnostic_text)

    def test_post_jsonp_decodes_callback_without_logging_values_or_body(self):
        callback = "callback_123"

        class JsonpResponse:
            status_code = 200
            headers = {"Content-Type": "application/javascript; charset=UTF-8"}
            url = "https://kyfw.12306.cn/passport/web/auth/uamtk?callback=CALLBACK_VALUE"
            encoding = "utf-8"
            text = f'{callback}({{"result_code":"0","newapptk":"PRIVATE_TICKET"}});'
            content = text.encode("utf-8")

        session = Session.__new__(Session)
        session._http = FakeHttp(JsonpResponse())
        session._lock = threading.Lock()

        with self.assertLogs("ny12306.http", level="DEBUG") as captured:
            result = session.post_jsonp(
                "https://kyfw.12306.cn/passport/web/auth/uamtk",
                callback=callback,
                data={"appid": "otn"},
            )

        method, _, kwargs = session._http.calls[0]
        self.assertEqual(method, "POST")
        self.assertEqual(kwargs["params"], {"callback": callback})
        self.assertEqual(kwargs["data"], {"appid": "otn"})
        self.assertEqual(result, {
            "result_code": "0",
            "newapptk": "PRIVATE_TICKET",
        })
        diagnostic_text = "\n".join(captured.output)
        self.assertIn("params_fields=callback", diagnostic_text)
        self.assertIn("data_fields=appid", diagnostic_text)
        self.assertNotIn("CALLBACK_VALUE", diagnostic_text)
        self.assertNotIn("PRIVATE_TICKET", diagnostic_text)
        self.assertNotIn(callback, diagnostic_text)

    def test_post_jsonp_error_reports_actual_method(self):
        session = Session.__new__(Session)
        session._http = FakeHttp(FakeResponse())
        session._lock = threading.Lock()
        with self.assertRaises(JsonResponseError) as error:
            session.post_jsonp(
                "https://kyfw.12306.cn/passport/web/auth/uamtk",
                callback="callback_123", data={"appid": "otn"},
            )
        self.assertEqual(error.exception.method, "POST")
        self.assertTrue(str(error.exception).startswith("POST "))


if __name__ == "__main__":
    unittest.main()
