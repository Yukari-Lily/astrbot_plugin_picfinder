"""HTTP/2 失败识别与浏览器响应包装测试。"""

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from browser_core.manager import BrowserResponse  # noqa: E402
from core.net import _looks_like_http2_failure  # noqa: E402


class TestHttp2Detection(unittest.TestCase):
    def test_yandex_reset_matches(self):
        exc = Exception(
            "Failed to perform, curl: (92) HTTP/2 stream 1 reset by server "
            "(error 0x2 INTERNAL_ERROR). See https://curl.se/libcurl/c/libcurl-errors.html"
        )
        self.assertTrue(_looks_like_http2_failure(exc))

    def test_other_errors_do_not_match(self):
        self.assertFalse(_looks_like_http2_failure(Exception("Connection refused")))
        self.assertFalse(_looks_like_http2_failure(TimeoutError("timed out")))
        self.assertFalse(_looks_like_http2_failure(Exception("Name or service not known")))


class TestBrowserResponse(unittest.TestCase):
    def test_header_is_case_insensitive(self):
        response = BrowserResponse(status=303, headers={"location": "https://x/y"}, url="")
        self.assertEqual(response.header("Location"), "https://x/y")
        self.assertEqual(response.header("missing", "d"), "d")


if __name__ == "__main__":
    unittest.main()
