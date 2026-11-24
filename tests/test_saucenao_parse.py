"""SauceNAO 解析测试（JSON 与网页两种返回）。"""

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from engines.saucenao import parse_saucenao_html, parse_saucenao_json  # noqa: E402


class TestSauceNAOParsing(unittest.TestCase):
    def test_json(self):
        payload = {
            "results": [
                {
                    "header": {"similarity": "96.52", "index_id": 5, "thumbnail": "https://img3.saucenao.com/a.jpg"},
                    "data": {
                        "ext_urls": ["https://www.pixiv.net/artworks/100000000"],
                        "title": "测试插画",
                        "member_name": "画师A",
                    },
                },
                {
                    "header": {"similarity": "20.0"},
                    "data": {"ext_urls": ["https://example.com/low"], "title": "低相似"},
                },
            ]
        }
        items = parse_saucenao_json(payload, threshold=65)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].platform, "Pixiv")
        self.assertEqual(items[0].author, "画师A")
        self.assertTrue(items[0].exact)
        self.assertAlmostEqual(items[0].similarity, 96.52)

    def test_html(self):
        page = """
        <div class="result"><table><tr>
          <td><a href="https://saucenao.com/search.php?db=999"><img src="https://img3.saucenao.com/a.jpg"></a></td>
          <td><div class="resultsimilarityinfo">91.20%</div>
          <div class="resulttitle"><strong>原图标题</strong></div>
          <div class="resultcontentcolumn"><strong>Author: </strong><a href="https://x.com/artist">artist</a><br>
          <a href="https://twitter.com/artist/status/123456">twitter</a></div></td>
        </tr></table></div>
        <div class="result" id="result-hidden-notification">hidden</div>
        """
        items = parse_saucenao_html(page, threshold=65)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].platform, "X")
        self.assertEqual(items[0].author, "artist")
        self.assertEqual(items[0].title, "原图标题")
        self.assertTrue(items[0].exact)


if __name__ == "__main__":
    unittest.main()
