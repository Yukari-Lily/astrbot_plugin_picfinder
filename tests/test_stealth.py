"""浏览器反检测辅助函数测试（不启动浏览器）。"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from browser_core.stealth import (  # noqa: E402
    GOOGLE_CONSENT_COOKIES,
    STEALTH_INIT_JS,
    chrome_user_agent,
    chrome_version,
    is_headless_user_agent,
    load_cookie_file,
    parse_cookie_json,
    parse_netscape_cookies,
)


class TestUserAgent(unittest.TestCase):
    def test_version_from_chromium_banner(self):
        self.assertEqual(chrome_version("Chromium 141.0.7390.37"), "141.0.7390.37")
        self.assertEqual(chrome_version("141.0.7390.37"), "141.0.7390.37")
        self.assertEqual(chrome_version("141.0.7390"), "141.0.7390")
        self.assertEqual(chrome_version(""), "")

    def test_user_agent_keeps_real_version(self):
        agent = chrome_user_agent("Chromium 139.0.7258.5")
        self.assertIn("Chrome/139.0.7258.5", agent)
        self.assertNotIn("Headless", agent)
        self.assertIn("Windows NT 10.0", agent)

    def test_user_agent_fallback(self):
        self.assertIn("Chrome/", chrome_user_agent(""))

    def test_detect_headless(self):
        self.assertTrue(is_headless_user_agent("Mozilla/5.0 HeadlessChrome/120.0.0.0"))
        self.assertFalse(is_headless_user_agent(chrome_user_agent("141.0.7390.37")))


class TestInitScript(unittest.TestCase):
    def test_patches_key_surfaces(self):
        for needle in ("webdriver", "window.chrome", "hardwareConcurrency", "permissions", "getParameter"):
            self.assertIn(needle, STEALTH_INIT_JS)

    def test_consent_cookies(self):
        names = {cookie["name"] for cookie in GOOGLE_CONSENT_COOKIES}
        self.assertIn("SOCS", names)
        for cookie in GOOGLE_CONSENT_COOKIES:
            self.assertEqual(cookie["domain"], ".google.com")
            self.assertTrue(cookie["secure"])


class TestCookieFiles(unittest.TestCase):
    NETSCAPE = (
        "# Netscape HTTP Cookie File\n"
        ".google.com\tTRUE\t/\tTRUE\t1893456000\tSID\tabc123\n"
        "#HttpOnly_.google.com\tTRUE\t/\tFALSE\t0\tHSID\tdef456\n"
        "bad line without tabs\n"
    )

    def test_netscape(self):
        records = parse_netscape_cookies(self.NETSCAPE)
        self.assertEqual(len(records), 2)
        by_name = {record["name"]: record for record in records}
        self.assertEqual(by_name["SID"]["value"], "abc123")
        self.assertEqual(by_name["SID"]["expires"], 1893456000)
        self.assertTrue(by_name["SID"]["secure"])
        # HttpOnly 行以 # 开头，但仍要解析
        self.assertEqual(by_name["HSID"]["value"], "def456")

    def test_json_playwright_list(self):
        payload = json.dumps(
            [
                {
                    "name": "SID",
                    "value": "abc",
                    "domain": ".google.com",
                    "path": "/",
                    "secure": True,
                    "sameSite": "no_restriction",
                }
            ]
        )
        records = parse_cookie_json(payload)
        self.assertEqual(records[0]["sameSite"], "None")

    def test_json_simple_map(self):
        records = parse_cookie_json(json.dumps({"SID": "abc"}))
        self.assertEqual(records[0]["domain"], ".google.com")

    def test_json_chrome_export(self):
        records = parse_cookie_json(json.dumps({"cookies": [{"name": "NID", "value": "1", "domain": ".google.com"}]}))
        self.assertEqual(records[0]["name"], "NID")

    def test_load_missing_return_empty(self):
        self.assertEqual(load_cookie_file(""), [])
        self.assertEqual(load_cookie_file("/nonexistent/cookies.txt"), [])

    def test_load_file(self):
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as handle:
            handle.write(self.NETSCAPE)
            path = handle.name
        try:
            self.assertEqual(len(load_cookie_file(path)), 2)
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
