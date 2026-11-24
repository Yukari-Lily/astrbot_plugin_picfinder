"""Ascii2D 结果解析测试。"""

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from engines.ascii2d import Ascii2DEngine, parse_ascii_html  # noqa: E402

PAGE = """
<html><body>
<div class="row item-box">
  <div class="col-xs-6"><img loading="lazy" src="/images/thumb_a.jpg"></div>
  <div class="detail-box gray-link">
    <h6><a href="https://www.pixiv.net/artworks/100000000">菜花黄名子ちゃん</a><small>pixiv</small></h6>
    <a href="https://www.pixiv.net/users/57190277">ちのすけ</a>
  </div>
</div>
<div class="row item-box">
  <div class="col-xs-6"><img loading="lazy" src="/images/thumb_b.jpg"></div>
  <div class="detail-box gray-link">
    <h6><a href="https://x.com/artist/status/123">作品</a><small>twitter</small></h6>
    <a href="https://x.com/artist">artist</a>
  </div>
</div>
<a href="/search/bovw/abcdef">特徴検索</a>
</body></html>
"""


class TestAscii2DParsing(unittest.TestCase):
    def test_color_results(self):
        items = parse_ascii_html(PAGE, limit=2, mode="color")
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0].url, "https://www.pixiv.net/artworks/100000000")
        self.assertEqual(items[0].extra["ascii_info"], "「菜花黄名子ちゃん」/「ちのすけ」")
        self.assertEqual(items[0].extra["ascii_mode"], "color")
        self.assertTrue(items[0].thumb.startswith("https://ascii2d.net/images/"))
        self.assertEqual(items[1].platform, "X")

    def test_bovw_url(self):
        self.assertEqual(
            Ascii2DEngine._bovw_url(PAGE, "https://ascii2d.net/search/file"),
            "https://ascii2d.net/search/bovw/abcdef",
        )

    def test_empty(self):
        self.assertEqual(parse_ascii_html("", limit=2), [])


if __name__ == "__main__":
    unittest.main()
