"""Google Lens 结果解析测试（不访问网络）。"""

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from engines.lens import parse_lens_dom, parse_lens_html  # noqa: E402


LENS_HTML = """
<html><body>
  <div class="YxbOwd">
    <table><tr><td class="GmoL0c"><div class="zVq10e">
      <img src="https://pbs.twimg.com/media/ABC.jpg" />
    </div></td>
    <td><a class="ngTNl" href="https://twitter.com/artist/status/1234567890">
      <div class="ZhosBf">作品标题 - Twitter</div>
      <span class="LbKnXb">x.com</span>
    </a></td></tr></table>
  </div>
  <div class="YxbOwd">
    <a class="ngTNl" href="https://www.pixiv.net/artworks/100000000">
      <div class="ZhosBf">Pixiv 原图</div>
    </a>
  </div>
  <div class="vEWxFf RCxtQc">
    <a href="https://example.com/similar"><span>相似图</span></a>
  </div>
</body></html>
"""


class TestLensParsing(unittest.TestCase):
    def test_exact_blocks(self):
        items = parse_lens_html(LENS_HTML, limit=10, exact=True)
        urls = [item.url for item in items]
        self.assertIn("https://x.com/artist/status/1234567890", urls)
        self.assertIn("https://www.pixiv.net/artworks/100000000", urls)
        self.assertTrue(all(item.exact for item in items if "similar" not in item.url))
        first = next(item for item in items if item.platform == "X")
        self.assertEqual(first.thumb, "https://pbs.twimg.com/media/ABC.jpg")

    def test_dom_payload(self):
        payload = {
            "items": [
                {"href": "https://x.com/a/status/42", "text": "@a on X: 标题", "img": ""},
                {"href": "https://www.google.com/search?q=x", "text": "导航", "img": ""},
                {"href": "https://www.pixiv.net/artworks/9", "text": "作品", "img": "https://i.pximg.net/x.jpg"},
            ]
        }
        items = parse_lens_dom(payload, limit=10, exact=True)
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0].url, "https://x.com/a/status/42")
        self.assertEqual(items[1].platform, "Pixiv")
        self.assertTrue(items[1].thumb.startswith("https://"))

    def test_js_shell_html_has_no_results(self):
        html = "<html><body><noscript>enablejs Please click here</noscript></body></html>"
        self.assertEqual(parse_lens_html(html, limit=5), [])


if __name__ == "__main__":
    unittest.main()
