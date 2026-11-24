"""平台识别与 URL 规范化测试。"""

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.platform import (  # noqa: E402
    classify,
    clean_url,
    first_line,
    image_key,
    is_blocked_url,
    unwrap_url,
)


class TestPlatform(unittest.TestCase):
    def test_x_status_canonical(self):
        info = classify("https://twitter.com/artist_name/status/1234567890?utm_source=foo&s=20")
        self.assertEqual(info.name, "X")
        self.assertEqual(info.canonical, "https://x.com/artist_name/status/1234567890")
        self.assertEqual(info.author_hint, "artist_name")

    def test_pixiv_artwork(self):
        info = classify("https://www.pixiv.net/en/artworks/100000000#manga")
        self.assertEqual(info.name, "Pixiv")
        self.assertEqual(info.canonical, "https://www.pixiv.net/artworks/100000000")
        self.assertEqual(info.item_id, "100000000")

    def test_pixiv_legacy(self):
        info = classify("https://www.pixiv.net/member_illust.php?mode=medium&illust_id=100000000")
        self.assertEqual(info.name, "Pixiv")
        self.assertEqual(info.canonical, "https://www.pixiv.net/artworks/100000000")
        self.assertEqual(info.item_id, "100000000")

    def test_bilibili(self):
        info = classify("https://www.bilibili.com/video/BV1GJ411x7h7?spm_id_from=333.999")
        self.assertEqual(info.name, "Bilibili")
        self.assertEqual(info.canonical, "https://www.bilibili.com/video/BV1GJ411x7h7")

    def test_youtube_short(self):
        info = classify("https://youtu.be/dQw4w9WgXcQ")
        self.assertEqual(info.platform if hasattr(info, "platform") else info.name, "YouTube")
        self.assertEqual(info.item_id, "dQw4w9WgXcQ")

    def test_danbooru_and_pinterest_weights(self):
        danbooru = classify("https://danbooru.donmai.us/posts/123")
        pinterest = classify("https://www.pinterest.com/pin/123456/")
        self.assertGreater(danbooru.weight, pinterest.weight)

    def test_unwrap_google_redirect(self):
        wrapped = "https://www.google.com/url?q=https%3A%2F%2Fx.com%2Fa%2Fstatus%2F1&sa=D"
        self.assertEqual(unwrap_url(wrapped), "https://x.com/a/status/1")
        self.assertEqual(clean_url(wrapped), "https://x.com/a/status/1")

    def test_blocked_urls(self):
        self.assertTrue(is_blocked_url("https://www.google.com/search?q=x"))
        self.assertTrue(is_blocked_url("javascript:alert(1)"))
        self.assertFalse(is_blocked_url("https://x.com/a/status/1"))
        self.assertFalse(is_blocked_url("https://notgoogle.com/photo"))

    def test_image_key_ignores_size_prefix(self):
        self.assertEqual(
            image_key("https://i.pinimg.com/236x/ab/cd/ef.jpg"),
            image_key("https://i.pinimg.com/originals/ab/cd/ef.jpg"),
        )
        self.assertEqual(
            image_key("https://pbs.twimg.com/media/ABC_normal.jpg"),
            image_key("https://pbs.twimg.com/media/ABC.jpg"),
        )
        self.assertEqual(
            image_key("https://pbs.twimg.com/media/ABC?format=jpg&name=small"),
            image_key("https://pbs.twimg.com/media/ABC.png"),
        )

    def test_first_line(self):
        self.assertEqual(first_line("第一行\n第二行"), "第一行")


if __name__ == "__main__":
    unittest.main()
