"""Integrated browser core tests that do not launch Chromium."""

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from browser_core import Snapshot, UrlRejected, format_snapshot, is_blank_page, normalize_url  # noqa: E402


class TestBrowserCore(unittest.TestCase):
    def test_url_normalization(self):
        self.assertEqual(normalize_url("example.com"), "https://example.com/")
        self.assertEqual(normalize_url("https://example.com/a?b=1#x"), "https://example.com/a?b=1")
        with self.assertRaises(UrlRejected):
            normalize_url("http://127.0.0.1:8080/")
        with self.assertRaises(UrlRejected):
            normalize_url("file:///etc/passwd")
        self.assertEqual(normalize_url("http://127.0.0.1:8080/", allow_local=True), "http://127.0.0.1:8080/")

    def test_snapshot_format(self):
        snapshot = Snapshot(
            url="https://example.com/",
            title="Example",
            text="hello",
            text_length=5,
            scroll={"y": 0, "height": 100, "viewport": 50},
            elements=[{"ref": "e1", "tag": "a", "name": "link", "href": "https://example.com/x"}],
            element_count=1,
        )
        text = format_snapshot(snapshot)
        self.assertIn("URL: https://example.com/", text)
        self.assertIn("- e1 a", text)
        self.assertIn("untrusted data", text)

    def test_blank_page_detection(self):
        # 打开 X 链接时只有一具空壳：不能把白图发到群里
        self.assertTrue(is_blank_page({"text": 0, "media": 0, "elements": 0, "height": 0}))
        self.assertTrue(is_blank_page({"text": 3, "media": 0, "elements": 2, "height": 40}))
        # 有正文 / 有图 / 有内容结构 / body 有高度 都不算空白
        self.assertFalse(is_blank_page({"text": 120, "media": 0, "elements": 0, "height": 0}))
        self.assertFalse(is_blank_page({"text": 0, "media": 2, "elements": 0, "height": 0}))
        self.assertFalse(is_blank_page({"text": 0, "media": 0, "elements": 40, "height": 0}))
        self.assertFalse(is_blank_page({"text": 0, "media": 0, "elements": 0, "height": 900}))
        # 拿不到统计信息时按「不空白」处理，别吞掉正常截图
        self.assertFalse(is_blank_page({}))
        self.assertFalse(is_blank_page(None))


if __name__ == "__main__":
    unittest.main()
