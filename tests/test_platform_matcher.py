"""平台识别与 URL 规范化单元测试。"""

import unittest
import sys
import os

# 将插件目录加入路径
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from normalizer.platform_matcher import (
    extract_platform_sources_from_text,
    normalize_platform_url,
)


class TestPlatformMatcher(unittest.TestCase):
    """测试各大互联网平台出处识别"""

    def test_twitter_x_status(self):
        url = "https://x.com/yukari_lily/status/1789012345678901234"
        norm = normalize_platform_url(url)
        self.assertEqual(norm.platform_name, "X (Twitter)")
        self.assertEqual(norm.author, "yukari_lily")
        self.assertEqual(norm.item_id, "1789012345678901234")
        self.assertTrue("🐦" in norm.badge)
        self.assertGreaterEqual(norm.priority_weight, 90)

    def test_twitter_legacy_domain(self):
        url = "https://twitter.com/illustrator_abc/status/987654321"
        norm = normalize_platform_url(url)
        self.assertEqual(norm.platform_name, "X (Twitter)")
        self.assertEqual(norm.author, "illustrator_abc")
        self.assertEqual(norm.canonical_url, "https://x.com/illustrator_abc/status/987654321")

    def test_bilibili_video(self):
        url = "https://www.bilibili.com/video/BV1xx411c7mD?spm_id_from=333.337"
        norm = normalize_platform_url(url)
        self.assertEqual(norm.platform_name, "哔哩哔哩")
        self.assertEqual(norm.item_id, "BV1xx411c7mD")
        self.assertEqual(norm.canonical_url, "https://www.bilibili.com/video/BV1xx411c7mD")
        self.assertTrue("📺" in norm.badge)

    def test_bilibili_opus(self):
        url = "https://www.bilibili.com/opus/9123456789"
        norm = normalize_platform_url(url)
        self.assertEqual(norm.platform_name, "哔哩哔哩")
        self.assertEqual(norm.item_id, "9123456789")

    def test_youtube_video(self):
        # standard watch
        norm1 = normalize_platform_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
        self.assertEqual(norm1.platform_name, "YouTube")
        self.assertEqual(norm1.item_id, "dQw4w9WgXcQ")
        self.assertEqual(norm1.canonical_url, "https://www.youtube.com/watch?v=dQw4w9WgXcQ")

        # youtu.be short link
        norm2 = normalize_platform_url("https://youtu.be/dQw4w9WgXcQ")
        self.assertEqual(norm2.platform_name, "YouTube")
        self.assertEqual(norm2.item_id, "dQw4w9WgXcQ")

        # shorts
        norm3 = normalize_platform_url("https://youtube.com/shorts/dQw4w9WgXcQ?feature=share")
        self.assertEqual(norm3.platform_name, "YouTube")
        self.assertEqual(norm3.item_id, "dQw4w9WgXcQ")

    def test_pixiv_artwork(self):
        url = "https://www.pixiv.net/artworks/112233445"
        norm = normalize_platform_url(url)
        self.assertEqual(norm.platform_name, "Pixiv")
        self.assertEqual(norm.item_id, "112233445")
        self.assertEqual(norm.canonical_url, "https://www.pixiv.net/artworks/112233445")
        self.assertTrue("🎨" in norm.badge)

    def test_artstation(self):
        url = "https://www.artstation.com/artwork/abcdef"
        norm = normalize_platform_url(url)
        self.assertEqual(norm.platform_name, "ArtStation")
        self.assertEqual(norm.item_id, "abcdef")

    def test_skeb(self):
        url = "https://skeb.jp/@artist_master/works/123"
        norm = normalize_platform_url(url)
        self.assertEqual(norm.platform_name, "Skeb")
        self.assertEqual(norm.author, "artist_master")
        self.assertEqual(norm.item_id, "123")

    def test_generic_web(self):
        url = "https://example.com/some/article"
        norm = normalize_platform_url(url)
        self.assertEqual(norm.platform_name, "example.com")
        self.assertEqual(norm.canonical_url, url)

    def test_extract_platform_sources_from_text(self):
        sample_text = (
            "推荐看看这个视频 https://www.bilibili.com/video/BV1xx411c7mD ，还有推特画师原图 "
            "https://x.com/yukari_lily/status/1789012345678901234 顺便Pixiv插画 https://www.pixiv.net/artworks/112233445"
        )
        sources = extract_platform_sources_from_text(sample_text)
        self.assertEqual(len(sources), 3)
        platforms = [s.platform_name for s in sources]
        self.assertIn("哔哩哔哩", platforms)
        self.assertIn("X (Twitter)", platforms)
        self.assertIn("Pixiv", platforms)

    def test_to_dict(self):
        norm = normalize_platform_url("https://x.com/yukari/status/999")
        d = norm.to_dict()
        self.assertEqual(d["platform"], "X (Twitter)")
        self.assertEqual(d["author"], "yukari")
        self.assertEqual(d["item_id"], "999")
        self.assertIn("canonical_url", d)


if __name__ == "__main__":
    unittest.main()
