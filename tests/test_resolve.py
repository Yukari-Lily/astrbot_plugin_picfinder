"""出处元数据解析测试（不访问网络）。"""

import asyncio
import io
import os
import sys
import unittest

from PIL import Image

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.image import ImageInput  # noqa: E402
from core.resolve import SourceResolver, _apply, parse_bilibili  # noqa: E402
from engines.base import Candidate  # noqa: E402
from core.resolve import (  # noqa: E402
    format_date,
    parse_danbooru,
    parse_fxtwitter,
    parse_oembed,
    parse_open_graph,
    parse_pixiv,
)


def pattern_image(seed=0, size=(240, 180)) -> bytes:
    image = Image.new("RGB", size)
    pixels = image.load()
    for x in range(size[0]):
        for y in range(size[1]):
            pixels[x, y] = ((x * 3 + y * 5 + seed) % 256, (x * 7 + y * 2 + seed) % 256, ((x // 12 + y // 12) % 2) * 200)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


class FakeClient:
    def __init__(self, data: bytes):
        self.data = data

    async def get_bytes(self, *args, **kwargs):
        return self.data


def crop_bytes(seed=0) -> bytes:
    image = Image.open(io.BytesIO(pattern_image(seed)))
    buffer = io.BytesIO()
    image.crop((30, 20, 200, 150)).save(buffer, format="PNG")
    return buffer.getvalue()


class TestVerifyPolicy(unittest.TestCase):
    def test_crop_verifies(self):
        resolver = SourceResolver(FakeClient(pattern_image()), verify=True)
        candidate = Candidate(
            url="https://x.com/a/status/1", engine="Google Lens", exact=True, image="https://img/x.jpg"
        )
        asyncio.run(resolver._verify(candidate, ImageInput.create(crop_bytes())))
        self.assertTrue(candidate.verified)
        self.assertEqual(candidate.extra.get("match"), "crop")

    def test_trusted_engine_mismatch_is_not_demoted(self):
        resolver = SourceResolver(FakeClient(pattern_image(seed=99)), verify=True)
        candidate = Candidate(
            url="https://x.com/a/status/1", engine="Google Lens", exact=True, image="https://img/x.jpg"
        )
        asyncio.run(resolver._verify(candidate, ImageInput.create(pattern_image())))
        self.assertTrue(candidate.exact)
        self.assertFalse(candidate.verified)
        self.assertEqual(candidate.extra.get("verify"), "mismatch_trusted")

    def test_yandex_mismatch_is_demoted(self):
        resolver = SourceResolver(FakeClient(pattern_image(seed=99)), verify=True)
        candidate = Candidate(url="https://example.com/post", engine="Yandex", exact=True, image="https://img/x.jpg")
        asyncio.run(resolver._verify(candidate, ImageInput.create(pattern_image())))
        self.assertFalse(candidate.exact)
        self.assertEqual(candidate.extra.get("verify"), "mismatch")


class TestResolveParsing(unittest.TestCase):
    def test_fxtwitter(self):
        payload = {
            "tweet": {
                "url": "https://x.com/artist/status/123",
                "text": "新作公开\n第二行",
                "created_at": "Tue Nov 24 04:21:02 +0000 2026",
                "author": {"screen_name": "artist", "name": "Artist", "url": "https://x.com/artist"},
                "media": {
                    "all": [
                        {
                            "url": "https://pbs.twimg.com/media/ABC.jpg",
                            "thumbnail_url": "https://pbs.twimg.com/media/ABC_thumb.jpg",
                        }
                    ]
                },
            }
        }
        meta = parse_fxtwitter(payload)
        self.assertEqual(meta["author"], "artist")
        self.assertEqual(meta["date"], "2026-11-24")
        self.assertEqual(meta["title"], "新作公开")
        self.assertEqual(meta["image"], "https://pbs.twimg.com/media/ABC.jpg")
        self.assertEqual(meta["images"], ["https://pbs.twimg.com/media/ABC.jpg"])

    def test_fxtwitter_multi_media(self):
        payload = {
            "tweet": {
                "author": {"screen_name": "a"},
                "text": "t",
                "media": {"all": [{"url": "https://pbs.twimg.com/1.jpg"}, {"url": "https://pbs.twimg.com/2.jpg"}]},
            }
        }
        self.assertEqual(
            parse_fxtwitter(payload)["images"], ["https://pbs.twimg.com/1.jpg", "https://pbs.twimg.com/2.jpg"]
        )

    def test_pixiv(self):
        payload = {
            "error": False,
            "body": {
                "illustId": "100000000",
                "illustTitle": "菜花黄名子酱",
                "userId": "12345",
                "userName": "画师B",
                "createDate": "2024-05-01T12:34:56+09:00",
                "urls": {"original": "https://i.pximg.net/orig.jpg", "thumb": "https://i.pximg.net/thumb.jpg"},
            },
        }
        meta = parse_pixiv(payload)
        self.assertEqual(meta["url"], "https://www.pixiv.net/artworks/100000000")
        self.assertEqual(meta["author"], "画师B")
        self.assertEqual(meta["author_url"], "https://www.pixiv.net/users/12345")
        self.assertEqual(meta["date"], "2024-05-01")
        self.assertEqual(meta["image"], "https://i.pximg.net/orig.jpg")
        self.assertEqual(meta["images"], ["https://i.pximg.net/orig.jpg"])

    def test_pixiv_multipage(self):
        payload = {
            "error": False,
            "body": {
                "illustId": "1",
                "pageCount": 3,
                "urls": {"original": "https://i.pximg.net/img-original/1_p0.jpg"},
            },
        }
        meta = parse_pixiv(payload)
        self.assertEqual(len(meta["images"]), 3)
        self.assertIn("https://i.pximg.net/img-original/1_p2.jpg", meta["images"])

    def test_pixiv_error(self):
        self.assertEqual(parse_pixiv({"error": True, "body": []}), {})

    def test_danbooru(self):
        meta = parse_danbooru(
            {
                "id": 9,
                "tag_string_artist": "some_artist other",
                "created_at": "2024-01-02T03:04:05.000-05:00",
                "tag_string_character": "char_a",
                "file_url": "https://cdn.donmai.us/x.jpg",
            }
        )
        self.assertEqual(meta["author"], "some artist other")
        self.assertEqual(meta["date"], "2024-01-02")
        self.assertEqual(meta["url"], "https://danbooru.donmai.us/posts/9")

    def test_oembed(self):
        meta = parse_oembed(
            {
                "title": "视频标题",
                "author_name": "UP主",
                "author_url": "https://youtube.com/@u",
                "thumbnail_url": "https://i.ytimg.com/x.jpg",
            }
        )
        self.assertEqual(meta["author"], "UP主")
        self.assertEqual(meta["title"], "视频标题")

    def test_open_graph(self):
        page = """
        <html><head>
          <meta property="og:title" content="页面标题" />
          <meta property="og:image" content="https://cdn.example.com/a.jpg" />
          <meta property="og:site_name" content="Example" />
          <meta property="article:published_time" content="2024-06-07T08:09:10Z" />
        </head></html>
        """
        meta = parse_open_graph(page)
        self.assertEqual(meta["title"], "页面标题")
        self.assertEqual(meta["image"], "https://cdn.example.com/a.jpg")
        self.assertEqual(meta["date"], "2024-06-07")

    def test_authoritative_metadata_overrides_engine_title(self):
        from engines.base import Candidate

        candidate = Candidate(url="https://www.pixiv.net/artworks/1", title="Creator: someone", author="wrong")
        _apply(candidate, {"title": "正确标题", "author": "正确画师", "_authoritative": True})
        self.assertEqual(candidate.title, "正确标题")
        self.assertEqual(candidate.author, "正确画师")

    def test_bilibili(self):
        meta = parse_bilibili(
            {
                "code": 0,
                "data": {
                    "bvid": "BV1GJ411x7h7",
                    "title": "视频标题",
                    "pubdate": 1700000000,
                    "pic": "http://i0.hdslb.com/x.jpg",
                    "owner": {"mid": 123, "name": "UP主"},
                },
            }
        )
        self.assertEqual(meta["author"], "UP主")
        self.assertEqual(meta["author_url"], "https://space.bilibili.com/123")
        self.assertEqual(meta["url"], "https://www.bilibili.com/video/BV1GJ411x7h7")
        self.assertTrue(meta["image"].startswith("https://"))
        self.assertEqual(meta["date"], "2023-11-15")

    def test_format_date_variants(self):
        self.assertEqual(format_date("2026-11-24T04:21:02+09:00"), "2026-11-24")
        self.assertEqual(format_date("Tue Nov 24 04:21:02 +0000 2026"), "2026-11-24")
        self.assertEqual(format_date(""), "")


if __name__ == "__main__":
    unittest.main()
