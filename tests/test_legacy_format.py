"""原版 picfinder_take 回复格式测试。"""

import asyncio
import io
import os
import sys
import unittest

from PIL import Image

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.legacy_format import (  # noqa: E402
    FAILURE_TEXT,
    build_legacy_blocks,
    decorate_thumb,
    group_candidates,
    info_text,
    result_text,
    service_name,
    similarity_value,
)
from engines.base import Candidate  # noqa: E402


class FakeClient:
    def __init__(self, data=None):
        self.data = data

    async def get_bytes(self, *args, **kwargs):
        return self.data


def png_bytes():
    buffer = io.BytesIO()
    Image.new("RGB", (48, 48), (20, 120, 200)).save(buffer, format="PNG")
    return buffer.getvalue()


class TestLegacyFormat(unittest.TestCase):
    def test_pixiv_service_and_info(self):
        candidate = Candidate(
            url="https://www.pixiv.net/artworks/100000000",
            platform="Pixiv",
            author="ちのすけ",
            title="菜花黄名子ちゃん",
            engine="SauceNAO",
            similarity=96.13,
            exact=True,
        )
        self.assertEqual(service_name(candidate), "pixiv")
        self.assertEqual(info_text(candidate), "「菜花黄名子ちゃん」/「ちのすけ」")
        self.assertEqual(
            result_text(candidate),
            "[pixiv][https://www.pixiv.net/artworks/100000000] 相似度:96.13%\n「菜花黄名子ちゃん」/「ちのすけ」",
        )

    def test_twitter_info(self):
        candidate = Candidate(
            url="https://x.com/artist/status/123",
            platform="X",
            author="artist",
            date="2026-11-24",
            engine="Google Lens",
            exact=True,
            verified=True,
        )
        self.assertEqual(service_name(candidate), "Twitter")
        self.assertEqual(info_text(candidate), "「2026-11-24」/「artist」")
        self.assertEqual(similarity_value(candidate), 100.0)

    def test_yandex_has_no_fake_similarity_and_is_last(self):
        yandex = Candidate(url="https://www.pixiv.net/artworks/1", platform="Pixiv", engine="Yandex", exact=True)
        ascii_item = Candidate(url="https://x.com/a/status/1", platform="X", engine="Ascii2D")
        groups = group_candidates([yandex, ascii_item], per_engine=2)
        self.assertEqual([engine for engine, _ in groups], ["Ascii2D", "Yandex"])
        self.assertNotIn("相似度", result_text(yandex))

    def test_ascii_blocks(self):
        candidate = Candidate(
            url="https://www.pixiv.net/artworks/1",
            platform="Pixiv",
            engine="Ascii2D",
            extra={"ascii_info": "「t」/「a」", "ascii_mode": "color"},
        )
        blocks = asyncio.run(build_legacy_blocks([candidate], FakeClient(None), thumb_on=False))
        self.assertEqual(blocks[0].text, "——色调检索——")
        self.assertEqual(blocks[1].text, "[pixiv][https://www.pixiv.net/artworks/1]\n「t」/「a」")

    def test_lens_failure_is_visible(self):
        sauce = Candidate(url="https://www.pixiv.net/artworks/1", platform="Pixiv", engine="SauceNAO", similarity=90)
        blocks = asyncio.run(
            build_legacy_blocks(
                [sauce], FakeClient(None), thumb_on=False, engine_errors={"Google Lens": "未安装 playwright"}
            )
        )
        self.assertIn("Google Lens 检索失败喵（没装浏览器）", blocks[0].text)
        self.assertEqual(blocks[1].text, "————>saucenao<————")

    def test_no_info(self):
        candidate = Candidate(url="https://example.com/x", engine="Yandex")
        self.assertEqual(info_text(candidate), "no info")

    def test_group_order_and_limit(self):
        candidates = [
            Candidate(url="https://www.pixiv.net/artworks/2", engine="SauceNAO", platform="Pixiv", similarity=80),
            Candidate(url="https://www.pixiv.net/artworks/1", engine="SauceNAO", platform="Pixiv", similarity=96),
            Candidate(url="https://x.com/a/status/1", engine="Google Lens", platform="X", exact=True),
            Candidate(url="https://www.pixiv.net/artworks/3", engine="SauceNAO", platform="Pixiv", similarity=70),
        ]
        groups = group_candidates(candidates, per_engine=2)
        self.assertEqual([engine for engine, _ in groups], ["Google Lens", "SauceNAO"])
        self.assertEqual(
            [item.url for item in groups[1][1]],
            [
                "https://www.pixiv.net/artworks/1",
                "https://www.pixiv.net/artworks/2",
            ],
        )

    def test_build_blocks_without_thumb(self):
        candidates = [
            Candidate(
                url="https://www.pixiv.net/artworks/1",
                platform="Pixiv",
                author="a",
                title="t",
                engine="SauceNAO",
                similarity=90,
                exact=True,
            ),
        ]
        blocks = asyncio.run(build_legacy_blocks(candidates, None, thumb_on=False))
        self.assertEqual(blocks[0].text, "————>saucenao<————")
        self.assertIn("[pixiv][https://www.pixiv.net/artworks/1] 相似度:90.00%", blocks[1].text)

    def test_build_blocks_with_thumb(self):
        candidate = Candidate(
            url="https://www.pixiv.net/artworks/1",
            platform="Pixiv",
            author="a",
            title="t",
            engine="SauceNAO",
            similarity=90,
            exact=True,
            thumb="https://img/x.jpg",
        )
        blocks = asyncio.run(build_legacy_blocks([candidate], FakeClient(png_bytes()), thumb_on=True))
        self.assertIsNotNone(blocks[1].thumb)
        self.assertNotIn("预览图下载失败", blocks[1].text)

    def test_build_blocks_thumb_failure(self):
        candidate = Candidate(
            url="https://www.pixiv.net/artworks/1",
            platform="Pixiv",
            author="a",
            title="t",
            engine="SauceNAO",
            similarity=90,
            exact=True,
            thumb="https://img/x.jpg",
        )
        blocks = asyncio.run(build_legacy_blocks([candidate], FakeClient(None), thumb_on=True))
        self.assertIsNone(blocks[1].thumb)
        self.assertTrue(blocks[1].text.startswith("[预览图下载失败]"))

    def test_failure(self):
        blocks = asyncio.run(build_legacy_blocks([], None, thumb_on=False))
        self.assertEqual(blocks[0].text, FAILURE_TEXT)

    def test_decorate_thumb(self):
        buffer = io.BytesIO()
        Image.new("RGB", (64, 64), (120, 30, 30)).save(buffer, format="JPEG")
        data = decorate_thumb(buffer.getvalue())
        self.assertIsNotNone(data)
        image = Image.open(io.BytesIO(data))
        self.assertEqual(image.size, (64, 64))


if __name__ == "__main__":
    unittest.main()


class TestSimilarFallback(unittest.TestCase):
    """一个精确出处都没有时，宁可按「相似」给出，也别只回一句失败。"""

    def test_mismatch_candidates_are_shown(self):
        candidate = Candidate(
            url="https://www.pixiv.net/artworks/100000001",
            platform="Pixiv",
            author="画师",
            title="标题",
            engine="SauceNAO",
            similarity=71.2,
            exact=False,
            extra={"verify": "mismatch"},
        )
        blocks = asyncio.run(build_legacy_blocks([candidate], FakeClient(), thumb_on=False))
        text = "\n".join(block.text for block in blocks)
        self.assertIn("只是相似图", text)
        self.assertIn("100000001", text)
        self.assertNotIn(FAILURE_TEXT, text)

    def test_strict_mode_still_filters(self):
        candidate = Candidate(
            url="https://www.pixiv.net/artworks/100000002",
            engine="SauceNAO",
            extra={"verify": "mismatch"},
        )
        self.assertEqual(group_candidates([candidate]), [])
