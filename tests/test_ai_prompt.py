"""AI 提炼与分析单元测试。"""

import unittest
import sys
import os

# 将插件目录加入路径
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from ai.multimodal_analyzer import ImageAnalysisResult, MultimodalAnalyzer
from engines.base import SearchResultItem, EngineSearchResult


class TestAIAnalysis(unittest.TestCase):
    """测试 AI 数据模型与逻辑"""

    def test_image_analysis_result(self):
        analysis = ImageAnalysisResult(
            subject_type="二次元插画",
            characters=["芙宁娜 (Furina)"],
            origin_work="原神 (Genshin Impact)",
            artist_or_author="画师名",
            ocr_text="watermark_123",
            search_keywords=["原神 芙宁娜 插画", "Furina Genshin artwork"],
        )
        self.assertEqual(analysis.origin_work, "原神 (Genshin Impact)")
        self.assertEqual(len(analysis.characters), 1)
        self.assertEqual(len(analysis.search_keywords), 2)


if __name__ == "__main__":
    unittest.main()
