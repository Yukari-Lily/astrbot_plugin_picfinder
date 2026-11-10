"""画师提取与作品检索单元测试。"""

import unittest
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from engines.base import EngineSearchResult, SearchResultItem
from normalizer.author_extractor import (
    ArtworkSample,
    AuthorArtworksManager,
    AuthorProfile,
)


class TestAuthorExtractor(unittest.TestCase):
    """测试画师提取与作品归纳"""

    def setUp(self):
        self.mgr = AuthorArtworksManager()

    def test_extract_author_from_saucenao_pixiv(self):
        sauce_res = EngineSearchResult(
            engine_name="SauceNAO",
            items=[
                SearchResultItem(
                    title="新立绘",
                    url="https://www.pixiv.net/artworks/109876543",
                    platform="Pixiv",
                    author="米山舞",
                    similarity=95.0,
                )
            ],
            best_similarity=95.0,
        )
        lens_res = EngineSearchResult(engine_name="Google Lens", items=[])

        profile = self.mgr.extract_author_profile([lens_res, sauce_res])
        self.assertEqual(profile.name, "米山舞")
        self.assertEqual(profile.primary_platform, "Pixiv")
        self.assertEqual(profile.danbooru_tag, "米山舞")

    def test_extract_author_from_lens_twitter(self):
        lens_res = EngineSearchResult(
            engine_name="Google Lens",
            items=[
                SearchResultItem(
                    title="Daily Sketch",
                    url="https://x.com/morikuraen/status/1789012345678901234",
                    platform="X (Twitter)",
                    author="morikuraen",
                    similarity=90.0,
                )
            ],
            best_similarity=90.0,
        )
        sauce_res = EngineSearchResult(engine_name="SauceNAO", items=[])

        profile = self.mgr.extract_author_profile([lens_res, sauce_res])
        self.assertEqual(profile.handle, "morikuraen")
        self.assertEqual(profile.primary_platform, "X (Twitter)")
        self.assertEqual(profile.profile_url, "https://x.com/morikuraen")
        self.assertEqual(profile.gallery_url, "https://x.com/morikuraen/media")

    def test_format_author_summary(self):
        profile = AuthorProfile(
            name="森倉円",
            primary_platform="X (Twitter)",
            handle="morikuraen",
            gallery_url="https://x.com/morikuraen/media",
            sample_artworks=[
                ArtworkSample(
                    title="少女插画 #1",
                    page_url="https://danbooru.donmai.us/posts/101",
                    image_url="https://cdn.example.com/sample1.jpg",
                    character_tags=["オリジナル"],
                    platform="Danbooru",
                ),
                ArtworkSample(
                    title="少女插画 #2",
                    page_url="https://danbooru.donmai.us/posts/102",
                    image_url="https://cdn.example.com/sample2.jpg",
                    platform="Danbooru",
                ),
            ],
        )
        text = self.mgr.format_author_summary(profile)
        self.assertIn("森倉円", text)
        self.assertIn("@morikuraen", text)
        self.assertIn("https://x.com/morikuraen/media", text)
        self.assertIn("少女插画 #1", text)
        self.assertIn("https://cdn.example.com/sample1.jpg", text)


if __name__ == "__main__":
    unittest.main()
