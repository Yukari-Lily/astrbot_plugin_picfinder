"""图片解码、上传预处理与多指纹匹配。

只用一个 dHash 核对「是不是同一张图」对裁切、镜像、反色、重滤镜都不够稳，
所以这里为每张图生成一组指纹：

* dHash：整图 + 左右翻转 + 4 档中心裁切 + 3×3 网格裁切；
* pHash：整图 + 2 档中心裁切（抗滤镜、抗轻微裁切）；
* aHash：整图（辅助判断）；
* 亮度均值/标准差：纯色或低细节图不靠哈希下结论。

比对时取「同组指纹」的最小距离，并区分 full / crop / filter / flip / invert，
这样既能把裁切/滤镜图认出来，也不会把随便一张相似图当成原图。
"""

from __future__ import annotations

import asyncio
import io
import math
from dataclasses import dataclass, field
from typing import Optional

from PIL import Image, ImageOps, UnidentifiedImageError

MAX_UPLOAD_BYTES = 8 * 1024 * 1024
MAX_INPUT_BYTES = 12 * 1024 * 1024
MAX_SIDE = 6144
HASH_SIZE = 8
_MASK64 = (1 << 64) - 1
_PHASH_SIZE = 32
_PHASH_LOW = 8

# 2D DCT 的余弦表（只算一次）
_COS = tuple(
    tuple(math.cos((2 * x + 1) * u * math.pi / (2 * _PHASH_SIZE)) for x in range(_PHASH_SIZE))
    for u in range(_PHASH_SIZE)
)

# 判定阈值（64 位哈希的汉明距离）
_FULL_EXACT = 8
_CROP_EXACT = 9
_PHASH_EXACT = 6
_AHASH_EXACT = 6
_NEAR = 14


def looks_like_image(data: bytes) -> bool:
    if not data or len(data) < 12:
        return False
    return (
        data.startswith(b"\xff\xd8\xff")  # jpeg
        or data.startswith(b"\x89PNG\r\n\x1a\n")  # png
        or data.startswith(b"GIF87a")
        or data.startswith(b"GIF89a")
        or (data.startswith(b"RIFF") and data[8:12] == b"WEBP")
        or data.startswith(b"BM")  # bmp
    )


def looks_blank(data: bytes, *, ratio: float = 0.99) -> bool:
    """截图上是不是「一整张白板」：几乎没有颜色变化。

    页面加载失败、被挡在 JS 空壳或纯色背景时，DOM 统计未必看得出来
    （例如空 body 但带 CSS 高度），像素这一层最直接。
    """
    try:
        image = _open(data).convert("RGB")
    except Exception:
        return False
    image.thumbnail((64, 64), Image.Resampling.BILINEAR)
    counts = image.getcolors(maxcolors=64 * 64 + 1) or []
    total = sum(count for count, _ in counts)
    if total:
        dominant = max(counts)[0]
        if dominant / total >= ratio:
            return True
    # 轻微渐变/抗锯齿的纯色页：通道极值也很小
    low, high = image.convert("L").getextrema()
    return high - low <= 8


def mime_for(data: bytes) -> str:
    if data.startswith(b"\x89PNG"):
        return "image/png"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def _open(data: bytes) -> Image.Image:
    img = Image.open(io.BytesIO(data))
    try:
        img.seek(0)
    except EOFError:
        pass
    return ImageOps.exif_transpose(img)


def _flatten(img: Image.Image) -> Image.Image:
    """把透明图铺到白底上，避免透明区域被当成黑色导致指纹偏差。"""
    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        background = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        return Image.alpha_composite(background, rgba).convert("RGB")
    return img.convert("RGB")


def _dhash(gray: Image.Image) -> int:
    small = gray.resize((HASH_SIZE + 1, HASH_SIZE), Image.Resampling.LANCZOS)
    pixels = small.tobytes()
    value = 0
    for row in range(HASH_SIZE):
        for col in range(HASH_SIZE):
            left = pixels[row * (HASH_SIZE + 1) + col]
            right = pixels[row * (HASH_SIZE + 1) + col + 1]
            value = (value << 1) | (1 if left > right else 0)
    return value


def _ahash(gray: Image.Image) -> int:
    small = gray.resize((HASH_SIZE, HASH_SIZE), Image.Resampling.LANCZOS)
    pixels = small.tobytes()
    average = sum(pixels) / len(pixels)
    value = 0
    for pixel in pixels:
        value = (value << 1) | (1 if pixel > average else 0)
    return value


def _phash(gray: Image.Image) -> int:
    """DCT 感知哈希，对滤镜、压缩、轻微裁切比 dHash 更稳。"""
    small = gray.resize((_PHASH_SIZE, _PHASH_SIZE), Image.Resampling.LANCZOS)
    pixels = small.tobytes()
    rows = []
    for y in range(_PHASH_SIZE):
        row = pixels[y * _PHASH_SIZE : (y + 1) * _PHASH_SIZE]
        rows.append([sum(row[x] * _COS[u][x] for x in range(_PHASH_SIZE)) for u in range(_PHASH_SIZE)])

    columns = []
    for u in range(_PHASH_SIZE):
        column = [rows[y][u] for y in range(_PHASH_SIZE)]
        columns.append([sum(column[y] * _COS[v][y] for y in range(_PHASH_SIZE)) for v in range(_PHASH_SIZE)])

    low = [columns[u][v] for v in range(_PHASH_LOW) for u in range(_PHASH_LOW) if not (u == 0 and v == 0)]
    median = sorted(low)[len(low) // 2]
    value = 0
    for coefficient in low:
        value = (value << 1) | (1 if coefficient > median else 0)
    return value


def _stats(gray: Image.Image) -> tuple[float, float]:
    small = gray.resize((_PHASH_SIZE, _PHASH_SIZE), Image.Resampling.LANCZOS)
    pixels = small.tobytes()
    mean = sum(pixels) / len(pixels)
    variance = sum((pixel - mean) ** 2 for pixel in pixels) / len(pixels)
    return mean, math.sqrt(variance)


def _center_crop(gray: Image.Image, ratio: float) -> Image.Image:
    width, height = gray.size
    crop_width = max(1, int(width * ratio))
    crop_height = max(1, int(height * ratio))
    left = (width - crop_width) // 2
    top = (height - crop_height) // 2
    return gray.crop((left, top, left + crop_width, top + crop_height))


def _crop_hashes(gray: Image.Image) -> list[int]:
    hashes = [_dhash(_center_crop(gray, ratio)) for ratio in (0.9, 0.8, 0.7, 0.6)]

    width, height = gray.size
    crop_width = max(1, int(width * 0.55))
    crop_height = max(1, int(height * 0.55))
    xs = sorted({0, (width - crop_width) // 2, width - crop_width})
    ys = sorted({0, (height - crop_height) // 2, height - crop_height})
    for top in ys:
        for left in xs:
            hashes.append(_dhash(gray.crop((left, top, left + crop_width, top + crop_height))))
    return hashes


def _crop_phashes(gray: Image.Image) -> list[int]:
    return [_phash(_center_crop(gray, ratio)) for ratio in (0.7, 0.6)]


def _invert(hash_value: int) -> int:
    return (~hash_value) & _MASK64


def _crop_search(template_gray: Image.Image, target_gray: Image.Image, aspect: float) -> int:
    """在目标图的多个尺度/位置上滑窗，用 dHash 找最像模板的裁切区域。"""
    width, height = target_gray.size
    if width < 8 or height < 8:
        return 64
    template = _dhash(template_gray)
    best = 64
    for step in range(17):
        scale = 0.30 + 0.04 * step
        window_width = max(8, int(width * scale))
        window_height = max(8, int(window_width / aspect))
        if window_height > height:
            window_height = height
            window_width = max(8, int(window_height * aspect))
        if window_width > width:
            continue
        xs = sorted({round(index * (width - window_width) / 8) for index in range(9)})
        ys = sorted({round(index * (height - window_height) / 8) for index in range(9)})
        for top in ys:
            for left in xs:
                value = _dhash(target_gray.crop((left, top, left + window_width, top + window_height)))
                distance = hash_distance(template, value)
                if distance < best:
                    best = distance
                    if best <= 3:
                        return best
    return best


def hash_distance(a: int, b: int) -> int:
    """汉明距离，0 表示完全一致（对缩放/重压缩鲁棒）。"""
    return int(a ^ b).bit_count()


@dataclass
class ImageMatch:
    exact: bool = False
    near: bool = False
    kind: str = ""
    distance: int = 64


def compare_images(a: "ImageInput", b: "ImageInput") -> ImageMatch:
    """多指纹比对：返回是否同一张图，以及是整图/裁切/滤镜/翻转/反色命中。"""
    if not (a.has_hash and b.has_hash):
        return ImageMatch()

    # 纯色/极低细节图：哈希会退化，改用亮度均值判断
    if min(a.detail, b.detail) < 4:
        if abs(a.mean - b.mean) <= 6:
            return ImageMatch(exact=True, near=True, kind="flat", distance=0)
        return ImageMatch()

    full = hash_distance(a.dhash, b.dhash)
    flip = hash_distance(a.dhash, b.flip_hash) if b.flip_hash else 64
    invert = hash_distance(a.dhash, _invert(b.dhash))

    crop = min(
        [hash_distance(a.dhash, value) for value in b.crop_hashes]
        + [hash_distance(value, b.dhash) for value in a.crop_hashes]
        + [64]
    )
    phash = hash_distance(a.phash, b.phash)
    ahash = hash_distance(a.ahash, b.ahash)
    phash_crop = min(
        [hash_distance(a.phash, value) for value in b.crop_phashes]
        + [hash_distance(value, b.phash) for value in a.crop_phashes]
        + [64]
    )

    if full <= _FULL_EXACT:
        return ImageMatch(exact=True, near=True, kind="full", distance=full)
    if flip <= _FULL_EXACT:
        return ImageMatch(exact=True, near=True, kind="flip", distance=flip)
    if invert <= _FULL_EXACT:
        return ImageMatch(exact=True, near=True, kind="invert", distance=invert)
    if crop <= _CROP_EXACT:
        return ImageMatch(exact=True, near=True, kind="crop", distance=crop)

    # 任意比例/偏移的裁切：在较小图作模板、较大图上滑窗
    if a.gray_small is not None and b.gray_small is not None:
        if a.width * a.height <= b.width * b.height:
            template, target, template_aspect = a.gray_small, b.gray_small, a.aspect
            reverse_template, reverse_target, reverse_aspect = b.gray_small, a.gray_small, b.aspect
        else:
            template, target, template_aspect = b.gray_small, a.gray_small, b.aspect
            reverse_template, reverse_target, reverse_aspect = a.gray_small, b.gray_small, a.aspect
        slide = _crop_search(template, target, template_aspect or 1.0)
        if slide > _CROP_EXACT and slide <= 20:
            slide = min(slide, _crop_search(reverse_template, reverse_target, reverse_aspect or 1.0))
        if slide <= _CROP_EXACT:
            return ImageMatch(exact=True, near=True, kind="crop", distance=slide)
        crop = min(crop, slide)

    if phash_crop <= _PHASH_EXACT:
        return ImageMatch(exact=True, near=True, kind="crop_filter", distance=phash_crop)
    if phash <= _PHASH_EXACT and ahash <= 12:
        return ImageMatch(exact=True, near=True, kind="filter", distance=phash)
    if phash <= 9 and ahash <= _AHASH_EXACT:
        return ImageMatch(exact=True, near=True, kind="filter", distance=phash)

    best_distance, best_kind = min(
        [
            (full, "full"),
            (flip, "flip"),
            (invert, "invert"),
            (crop, "crop"),
            (phash_crop, "crop_filter"),
            (phash, "filter"),
        ],
        key=lambda item: item[0],
    )
    if best_distance <= _NEAR:
        return ImageMatch(exact=False, near=True, kind=best_kind, distance=best_distance)
    return ImageMatch(exact=False, near=False, kind=best_kind, distance=best_distance)


@dataclass
class ImageInput:
    """统一后的待检索图片。"""

    data: bytes
    mime: str = "image/jpeg"
    filename: str = "image.jpg"
    source_url: str = ""
    width: int = 0
    height: int = 0
    dhash: int = 0
    has_hash: bool = False
    extra: dict = field(default_factory=dict)
    # 多指纹
    flip_hash: int = 0
    ahash: int = 0
    phash: int = 0
    crop_hashes: list[int] = field(default_factory=list)
    crop_phashes: list[int] = field(default_factory=list)
    aspect: float = 1.0
    mean: float = 0.0
    detail: float = 0.0
    gray_small: Optional[Image.Image] = field(default=None, repr=False, compare=False)

    @classmethod
    def create(cls, data: bytes, mime: str = "", source_url: str = "") -> "ImageInput":
        if not looks_like_image(data):
            raise ValueError("不是可识别的图片格式")
        try:
            img = _open(data)
            gray = _flatten(img).convert("L")
            width, height = gray.size
            mean, detail = _stats(gray)
            gray_small = gray.copy()
            gray_small.thumbnail((64, 64), Image.Resampling.LANCZOS)
            signature = {
                "dhash": _dhash(gray),
                "flip_hash": _dhash(gray.transpose(Image.Transpose.FLIP_LEFT_RIGHT)),
                "ahash": _ahash(gray),
                "phash": _phash(gray),
                "crop_hashes": _crop_hashes(gray),
                "crop_phashes": _crop_phashes(gray),
            }
        except (UnidentifiedImageError, OSError, ValueError) as exc:
            raise ValueError(f"图片解码失败: {exc}") from exc

        mime = mime or mime_for(data)
        ext = {"image/png": ".png", "image/gif": ".gif", "image/webp": ".webp"}.get(mime, ".jpg")
        return cls(
            data=data,
            mime=mime,
            filename=f"image{ext}",
            source_url=source_url,
            width=width,
            height=height,
            dhash=signature["dhash"],
            has_hash=True,
            flip_hash=signature["flip_hash"],
            ahash=signature["ahash"],
            phash=signature["phash"],
            crop_hashes=signature["crop_hashes"],
            crop_phashes=signature["crop_phashes"],
            aspect=(width / height) if height else 1.0,
            mean=mean,
            detail=detail,
            gray_small=gray_small,
        )

    @classmethod
    async def load(
        cls,
        client,
        *,
        url: Optional[str] = None,
        path: Optional[str] = None,
        max_bytes: int = MAX_INPUT_BYTES,
    ) -> "ImageInput":
        if path:
            data = await asyncio.to_thread(_read_file, path)
            return cls.create(data, source_url=url or "")

        if not url:
            raise ValueError("缺少图片地址")

        data = await client.get_bytes(url, max_bytes=max_bytes)
        if not data:
            raise ValueError("图片下载失败")
        return cls.create(data, source_url=url)

    def upload_parts(
        self,
        *,
        max_bytes: int = 0,
        max_side: int = 0,
    ) -> tuple[bytes, str, str]:
        """返回 (字节, MIME, 文件名)，重编码时同步修正 MIME/扩展名。

        各引擎的体积上限并不一致：Yandex 收到几 MB 的 PNG 会直接 413/502，
        所以允许调用方传更小的上限，必要时压成 JPEG。
        """
        payload = self.payload(max_bytes=max_bytes, max_side=max_side)
        if payload is self.data:
            return payload, self.mime, self.filename
        stem = self.filename.rsplit(".", 1)[0] if "." in self.filename else self.filename
        return payload, "image/jpeg", f"{stem}.jpg"

    def payload(self, *, max_bytes: int = 0, max_side: int = 0) -> bytes:
        """返回适合上传的图片字节：过大/过宽时重编码为 JPEG。"""
        limit = max_bytes or MAX_UPLOAD_BYTES
        side = max_side or MAX_SIDE
        if len(self.data) <= limit and max(self.width, self.height) <= side:
            return self.data
        try:
            img = _flatten(_open(self.data))
            img.thumbnail((min(side, 2048), min(side, 2048)), Image.Resampling.LANCZOS)
        except Exception:
            return self.data

        best = self.data
        for quality, scale in ((90, 1.0), (85, 0.85), (80, 0.7), (72, 0.55)):
            try:
                candidate = img
                if scale < 1.0:
                    candidate = img.copy()
                    candidate.thumbnail(
                        (int(img.width * scale), int(img.height * scale)),
                        Image.Resampling.LANCZOS,
                    )
                buffer = io.BytesIO()
                candidate.save(buffer, format="JPEG", quality=quality, optimize=True)
                best = buffer.getvalue()
            except Exception:
                continue
            if len(best) <= limit:
                break
        return best


def _read_file(path: str) -> bytes:
    with open(path, "rb") as handle:
        return handle.read()
