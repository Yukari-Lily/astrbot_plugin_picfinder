"""图片指纹与上传预处理测试。"""

import io
import os
import sys
import unittest

from PIL import Image, ImageFilter, ImageOps

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.image import looks_blank, ImageInput, compare_images, hash_distance, looks_like_image, mime_for  # noqa: E402


def make_png(size=(64, 64), color=(180, 40, 40)) -> bytes:
    image = Image.new("RGB", size, color)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def make_pattern(size=(320, 240), seed=0) -> Image.Image:
    image = Image.new("RGB", size)
    pixels = image.load()
    for x in range(size[0]):
        for y in range(size[1]):
            pixels[x, y] = (
                (x * 3 + y * 5 + seed * 37) % 256,
                (x * 7 + y * 2 + seed * 11) % 256,
                ((x // 16 + y // 16 + seed) % 2) * 180,
            )
    return image


def image_bytes(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


class TestImage(unittest.TestCase):
    def test_create_and_hash(self):
        data = make_png()
        self.assertTrue(looks_like_image(data))
        self.assertEqual(mime_for(data), "image/png")
        image = ImageInput.create(data)
        self.assertEqual((image.width, image.height), (64, 64))
        self.assertTrue(image.has_hash)
        self.assertEqual(image.filename, "image.png")

    def test_hash_stable_across_resize_and_jpeg(self):
        original = ImageInput.create(make_png((256, 256)))
        resized = Image.new("RGB", (96, 96), (180, 40, 40))
        buffer = io.BytesIO()
        resized.save(buffer, format="JPEG", quality=70)
        other = ImageInput.create(buffer.getvalue())
        self.assertLessEqual(hash_distance(original.dhash, other.dhash), 4)

    def test_different_colors_differ(self):
        red = ImageInput.create(make_png(color=(200, 20, 20)))
        # 构造有明显明暗变化的图，避免纯色图 dHash 全 0 的退化情况
        gradient = Image.new("L", (64, 64))
        for x in range(64):
            for y in range(64):
                gradient.putpixel((x, y), 255 if x % 2 == 0 else 0)
        buffer = io.BytesIO()
        gradient.convert("RGB").save(buffer, format="PNG")
        other = ImageInput.create(buffer.getvalue())
        self.assertGreaterEqual(hash_distance(red.dhash, other.dhash), 8)

    def test_reject_non_image(self):
        with self.assertRaises(ValueError):
            ImageInput.create(b"not an image at all")

    def test_crop_is_recognized(self):
        original = make_pattern()
        cropped = original.crop((40, 30, 250, 210))
        match = compare_images(ImageInput.create(image_bytes(cropped)), ImageInput.create(image_bytes(original)))
        self.assertTrue(match.exact)
        self.assertIn(match.kind, {"crop", "crop_filter", "full"})

    def test_offcenter_crop_is_recognized(self):
        original = make_pattern()
        cropped = original.crop((0, 0, 175, 130))
        match = compare_images(ImageInput.create(image_bytes(cropped)), ImageInput.create(image_bytes(original)))
        self.assertTrue(match.exact)

    def test_flip_is_recognized(self):
        original = make_pattern()
        flipped = original.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
        match = compare_images(ImageInput.create(image_bytes(flipped)), ImageInput.create(image_bytes(original)))
        self.assertTrue(match.exact)
        self.assertEqual(match.kind, "flip")

    def test_invert_is_recognized(self):
        original = make_pattern()
        inverted = ImageOps.invert(original)
        match = compare_images(ImageInput.create(image_bytes(inverted)), ImageInput.create(image_bytes(original)))
        self.assertTrue(match.exact)
        self.assertIn(match.kind, {"invert", "filter", "full"})

    def test_filter_is_recognized(self):
        original = make_pattern()
        filtered = original.filter(ImageFilter.GaussianBlur(1.5))
        match = compare_images(ImageInput.create(image_bytes(filtered)), ImageInput.create(image_bytes(original)))
        self.assertTrue(match.exact)

    def test_different_image_not_exact(self):
        first = make_pattern(seed=0)
        second = make_pattern(seed=9)
        match = compare_images(ImageInput.create(image_bytes(first)), ImageInput.create(image_bytes(second)))
        self.assertFalse(match.exact)

    def test_flat_color_guard(self):
        red = ImageInput.create(make_png(color=(200, 20, 20)))
        blue = ImageInput.create(make_png(color=(20, 20, 200)))
        self.assertFalse(compare_images(red, blue).exact)

    def test_multi_fingerprints_present(self):
        image = ImageInput.create(image_bytes(make_pattern()))
        self.assertGreaterEqual(len(image.crop_hashes), 10)
        self.assertEqual(len(image.crop_phashes), 2)
        self.assertNotEqual(image.flip_hash, 0)
        self.assertNotEqual(image.phash, 0)

    def test_payload_keeps_small_original(self):
        data = make_png()
        image = ImageInput.create(data)
        self.assertEqual(image.payload(), data)


class TestBlankScreenshot(unittest.TestCase):
    def _png(self, color, size=(400, 300)):
        buffer = io.BytesIO()
        Image.new("RGB", size, color).save(buffer, format="PNG")
        return buffer.getvalue()

    def test_pure_white_is_blank(self):
        self.assertTrue(looks_blank(self._png((255, 255, 255))))

    def test_solid_color_is_blank(self):
        self.assertTrue(looks_blank(self._png((18, 18, 18))))

    def test_content_is_not_blank(self):
        buffer = io.BytesIO()
        image = Image.new("RGB", (400, 300), (255, 255, 255))
        image.paste((20, 20, 20), (40, 40, 360, 260))
        image.save(buffer, format="PNG")
        self.assertFalse(looks_blank(buffer.getvalue()))

    def test_garbage_is_not_blank(self):
        self.assertFalse(looks_blank(b"not an image at all"))


if __name__ == "__main__":
    unittest.main()
