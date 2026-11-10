"""单元测试：AI 自动全网联网检索引擎与关键词提炼。"""

import asyncio
import os
import sys
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from ai.multimodal_analyzer import ImageAnalysisResult
from engines.base import SearchResultItem
from engines.web_search import (
    WebSearchEngine,
    clean_watermark_text,
    to_simplified_variant,
)
from normalizer.platform_matcher import (
    extract_author_hint,
    normalize_platform_url,
)


class TestWebSearchEngine(unittest.TestCase):
    """测试网络检索工具与关键词生成逻辑"""

    def setUp(self):
        self.engine = WebSearchEngine(proxy="")

    def test_clean_watermark_and_variants(self):
        # 1. 清理表情符号与水印杂质
        self.assertEqual(clean_watermark_text("小黑♡"), "小黑")
        self.assertEqual(clean_watermark_text("小藍及…"), "小藍")
        self.assertEqual(clean_watermark_text("★摄影_阿强★"), "摄影 阿强")

        # 2. 繁简转换候补
        self.assertEqual(to_simplified_variant("小藍"), "小蓝")
        self.assertEqual(to_simplified_variant("櫻花"), "樱花")
        self.assertEqual(to_simplified_variant("普通文字"), "")

    def test_generate_queries_himeko(self):
        analysis = ImageAnalysisResult(
            subject_type="真人Cos/同人演绎",
            characters=["姬子"],
            origin_work="《崩坏：星穹铁道》",
            ocr_text="小黑♡",
            search_keywords=["姬子 小黑♡", "Himeko cos 小黑"],
        )
        queries = self.engine.generate_queries_from_analysis(analysis)
        self.assertIn("姬子 cos 小黑", queries)
        self.assertIn("崩坏：星穹铁道 姬子 小黑", queries)
        self.assertTrue(any("Himeko" in q for q in queries))

    def test_generate_queries_tohsaka(self):
        analysis = ImageAnalysisResult(
            subject_type="个人 Cosplay 照片",
            characters=["远坂凛", "芳泽霞"],
            origin_work="Fate",
            ocr_text="小藍及…",
            search_keywords=["小藍 远坂凛 Cos"],
        )
        queries = self.engine.generate_queries_from_analysis(analysis)
        self.assertTrue(any("远坂凛 cos 小藍" in q for q in queries))
        self.assertTrue(any("远坂凛 cos 小蓝" in q for q in queries))

    def test_new_platforms_matcher(self):
        # 抖音
        norm_dy = normalize_platform_url("https://www.douyin.com/note/7672391422704083274")
        self.assertEqual(norm_dy.platform_name, "抖音")
        self.assertIn("抖音", norm_dy.badge)

        # 米游社
        norm_my = normalize_platform_url("https://www.miyoushe.com/sr/article/53812473")
        self.assertEqual(norm_my.platform_name, "米游社")

        # 小红书
        norm_xhs = normalize_platform_url("https://www.xiaohongshu.com/explore/65e9987f0000000001020304")
        self.assertEqual(norm_xhs.platform_name, "小红书")

        # Bilibili 专栏
        norm_cv = normalize_platform_url("https://www.bilibili.com/read/cv123456")
        self.assertEqual(norm_cv.platform_name, "哔哩哔哩")
        self.assertIn("cv123456", norm_cv.item_id)

    def test_extract_author_hint(self):
        title = "星穹铁道姬子cos合集，看看有你喜欢的吗_哔哩哔哩_bilibili"
        snippet = "视频作者 Cosplay美女鉴赏, 作者简介 分享高颜值角色扮演"
        author = extract_author_hint(title, snippet)
        self.assertEqual(author, "Cosplay美女鉴赏")

        # X on
        self.assertEqual(extract_author_hint("dirya on X: Tohsaka", ""), "dirya")

    def test_search_by_queries_dedup(self):
        async def run_test():
            item1 = SearchResultItem(title="星穹铁道 姬子cos", url="https://www.bilibili.com/video/BV1zT421y7ZF", platform="哔哩哔哩")
            item2 = SearchResultItem(title="星穹铁道 姬子cos", url="https://www.bilibili.com/video/BV1zT421y7ZF", platform="哔哩哔哩")
            item3 = SearchResultItem(title="姬子出处", url="https://www.miyoushe.com/sr/article/53812473", platform="米游社")

            with patch.object(self.engine, "search_ddg", new_callable=AsyncMock) as mock_ddg:
                mock_ddg.side_effect = [[item1, item2], [item3]]
                res = await self.engine.search_by_queries(["query1", "query2"], max_items=5)
                self.assertEqual(len(res.items), 2)  # item1 and item3 (item2 deduped)
                self.assertEqual(res.items[0].url, "https://www.bilibili.com/video/BV1zT421y7ZF")
                self.assertEqual(res.items[1].url, "https://www.miyoushe.com/sr/article/53812473")

        asyncio.run(run_test())


if __name__ == "__main__":
    unittest.main()
