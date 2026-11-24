"""Yandex 收录页解析测试（不访问网络）。"""

import html
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from engines.yandex import extract_cbir_state, parse_cbir_sites  # noqa: E402

STATE = {
    "initialState": {
        "cbirSites": {
            "sites": [
                {
                    "title": "作者原推 - X",
                    "description": "2024 年的原图",
                    "url": "https://x.com/artist/status/123456?utm_medium=organic",
                    "domain": "x.com",
                    "originalImage": {"url": "https://pbs.twimg.com/media/ABC.jpg", "width": 1200, "height": 1600},
                    "thumb": {"url": "//img.yandex.net/thumb.jpg"},
                },
                {
                    "title": "Pixiv 作品",
                    "url": "https://www.pixiv.net/artworks/100000000",
                    "domain": "pixiv.net",
                    "originalImage": {
                        "url": "https://i.pximg.net/img-original/img/2024/01/01/00/00/00/100000000_p0.jpg",
                        "width": 800,
                        "height": 1000,
                    },
                    "thumb": {"url": "//img.yandex.net/pixiv.jpg"},
                },
                {
                    "title": "同图转载",
                    "url": "https://example.com/copy",
                    "domain": "example.com",
                    "originalImage": {"url": "https://pbs.twimg.com/media/ABC.jpg", "width": 1200, "height": 1600},
                    "thumb": {"url": "//img.yandex.net/copy.jpg"},
                },
            ]
        }
    }
}


class TestYandexParsing(unittest.TestCase):
    def test_extract_and_parse(self):
        page = f'<div class="Root" data-state="{html.escape(json.dumps(STATE))}"></div>'
        state = extract_cbir_state(page)
        self.assertIsNotNone(state)
        items = parse_cbir_sites(state, limit=10)
        # 第三张与第一张是同一张图，应被去重
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0].url, "https://x.com/artist/status/123456")
        self.assertEqual(items[0].platform, "X")
        self.assertEqual(items[1].platform, "Pixiv")
        self.assertTrue(items[0].exact)
        self.assertTrue(items[0].thumb.startswith("https://"))

    def test_missing_state(self):
        self.assertIsNone(extract_cbir_state("<html><body>captcha</body></html>"))


if __name__ == "__main__":
    unittest.main()
