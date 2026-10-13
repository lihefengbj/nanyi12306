import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import requests

from core.session import BASE_URL, Session


class CookiePersistenceTests(unittest.TestCase):
    def make_session(self):
        session = Session.__new__(Session)
        session._http = requests.Session()
        session._lock = threading.Lock()
        return session

    def test_roundtrip_keeps_same_name_cookies_separate(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "cookies.json"
            before = self.make_session()
            for path, value in [
                ("/passport", "AUTH_SESSION"), ("/otn", "BUSINESS_SESSION"),
            ]:
                cookie = requests.cookies.create_cookie(
                    "JSESSIONID", value, domain="kyfw.12306.cn", path=path,
                    secure=True, rest={"HttpOnly": True},
                )
                cookie.domain_specified = False
                before._http.cookies.set_cookie(cookie)
            with patch("core.session.data_file", return_value=cache):
                before.save_cookies()
                after = self.make_session()
                after.load_cookies()
            self.assertEqual(len(after._http.cookies), 2)
            for path, expected in [
                ("/passport/web/auth/uamtk", "AUTH_SESSION"),
                ("/otn/login/checkUser", "BUSINESS_SESSION"),
            ]:
                request = after._http.prepare_request(
                    requests.Request("POST", BASE_URL + path)
                )
                self.assertEqual(
                    request.headers.get("Cookie"), f"JSESSIONID={expected}",
                )
            for cookie in after._http.cookies:
                self.assertFalse(cookie.domain_specified)
                self.assertTrue(cookie.secure)
                self.assertEqual(cookie._rest, {"HttpOnly": True})

    def test_legacy_name_value_cache_is_not_restored_unscoped(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "cookies.json"
            # Construct the legacy cache through the old serializer, without
            # ever reading or overwriting the user's real cache.
            session = self.make_session()
            session._http.cookies.set("JSESSIONID", "LEGACY_SESSION")
            with patch("core.session.data_file", return_value=cache):
                with patch("core.session.json.dumps",
                           return_value=json.dumps({"JSESSIONID": "LEGACY_SESSION"})):
                    session.save_cookies()
                restored = self.make_session()
                restored.load_cookies()
            self.assertEqual(len(restored._http.cookies), 0)

    def test_restored_cookies_do_not_leak_to_external_host(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "cookies.json"
            before = self.make_session()
            before._http.cookies.set(
                "JSESSIONID", "SCOPED_SESSION", domain="kyfw.12306.cn", path="/otn"
            )
            with patch("core.session.data_file", return_value=cache):
                before.save_cookies()
                after = self.make_session()
                after.load_cookies()
            request = after._http.prepare_request(
                requests.Request("GET", "https://example.com/otn")
            )
            self.assertNotIn("Cookie", request.headers)


if __name__ == "__main__":
    unittest.main()
