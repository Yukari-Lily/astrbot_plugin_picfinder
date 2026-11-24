"""上传前压缩测试：Yandex 之类对体积敏感的入口要能拿到小图。"""

import os
import sys
import unittest

from PIL import Image

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.image import ImageInput  # noqa: E402


def noisy_png(size: int = 1600) -> bytes:
    """不可压缩的 PNG，用来模拟几 MB 的原图。"""
    import io

    raw = os.urandom(size * size * 3)
    image = Image.frombytes("RGB", (size, size), raw)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


class TestUploadParts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.png = noisy_png()
        cls.image = ImageInput.create(cls.png)

    def test_small_limit_recompresses_to_jpeg(self):
        data, mime, filename = self.image.upload_parts(max_bytes=900 * 1024, max_side=1200)
        self.assertLessEqual(len(data), 900 * 1024)
        self.assertEqual(mime, "image/jpeg")
        self.assertTrue(filename.endswith(".jpg"))
        self.assertLess(max(Image.open(__import__("io").BytesIO(data)).size), 1300)

    def test_default_keeps_original(self):
        data, mime, _ = self.image.upload_parts()
        self.assertEqual(data, self.png)
        self.assertEqual(mime, self.image.mime)

    def test_big_limit_keeps_png(self):
        data, mime, _ = self.image.upload_parts(max_bytes=8 * 1024 * 1024, max_side=2048)
        self.assertEqual(data, self.png)
        self.assertEqual(mime, "image/png")


if __name__ == "__main__":
    unittest.main()
