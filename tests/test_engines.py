"""搜索引擎基础与数据模型单元测试。"""

import unittest
import sys
import os
import asyncio

# 将插件目录加入路径
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from engines.base import SearchResultItem, EngineSearchResult
from engines.saucenao import SauceNAOEngine
from engines.google_lens import GoogleLensEngine


class TestEngines(unittest.TestCase):
    """测试引擎数据结构与基本解析"""

    def test_search_result_item_summary(self):
        item = SearchResultItem(
            title="测试插画",
            url="https://x.com/illustrator/status/123456",
            platform="X (Twitter)",
            author="illustrator",
            similarity=92.5,
            source_engine="Google Lens",
            is_exact_match=True,
        )
        line = item.to_summary_line()
        self.assertIn("[X (Twitter)]", line)
        self.assertIn("@illustrator", line)
        self.assertIn("「测试插画」", line)
        self.assertIn("92.5%", line)
        self.assertIn("https://x.com/illustrator/status/123456", line)

    def test_saucenao_title_author_extraction(self):
        engine = SauceNAOEngine(api_key="", threshold=70.0)
        # 模拟 Pixiv
        header = {"index_id": 5}
        data = {"title": "夜景", "member_name": "画师A"}
        title, author = engine._extract_sauce_title_author(header, data)
        self.assertEqual(title, "夜景")
        self.assertEqual(author, "画师A")

        # 模拟 Twitter
        header = {"index_id": 41}
        data = {"twitter_user_handle": "tw_artist", "title": ""}
        title, author = engine._extract_sauce_title_author(header, data)
        self.assertEqual(author, "tw_artist")

    def test_google_lens_json_extraction(self):
        engine = GoogleLensEngine()
        mock_data = [
            ["ignored", 123],
            [
                "https://x.com/artist/status/12345678",
                "Fantastic Art - Twitter",
                "https://t.co/thumb.jpg",
            ],
            [
                "https://www.bilibili.com/video/BV1234567890",
                "原神角色演示视频",
            ]
        ]
        results = engine._extract_lens_items_from_json(mock_data, max_results=3)
        self.assertGreaterEqual(len(results), 2)
        urls = [r[0] for r in results]
        self.assertIn("https://x.com/artist/status/12345678", urls)
        self.assertIn("https://www.bilibili.com/video/BV1234567890", urls)


if __name__ == "__main__":
    unittest.main()
