"""按原版 HoshinoBot `picfinder_take` 的回复格式生成消息块。

原版格式（合并转发，每条结果一个节点）：

    ————>saucenao<————
    [缩略图]
    [pixiv][https://...] 相似度:96.13%
    「标题」/「画师」

这里只负责生成「文本 + 缩略图字节」块，具体转成 AstrBot 消息组件由 main 完成。
"""

from __future__ import annotations

import asyncio
import io
import random
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

from PIL import Image

try:
    from ..engines.base import Candidate  # type: ignore
except (ImportError, ValueError):  # pragma: no cover
    from engines.base import Candidate  # type: ignore

from .platform import classify, first_line, is_blocked_url, is_listing_url

ENGINE_ORDER = ("Google Lens", "SauceNAO", "Ascii2D", "TraceMoe", "Yandex")
ENGINE_HEADERS = {
    "Google Lens": "————>google lens<————",
    "SauceNAO": "————>saucenao<————",
    "Ascii2D": "————>ascii2d<————",
    "TraceMoe": "————>tracemoe<————",
    "Yandex": "————>yandex<————",
}
FAILURE_TEXT = "呜呜呜，都检索失败喵，牡蛎~"

# 原版 sauces_info 的服务名风格
SERVICE_NAMES = {
    "Pixiv": "pixiv",
    "X": "Twitter",
    "Danbooru": "Danbooru",
    "Gelbooru": "Gelbooru",
    "DeviantArt": "deviantArt",
    "Bilibili": "bilibili",
    "YouTube": "YouTube",
    "ArtStation": "Artstation",
    "Skeb": "Skeb",
    "Fanbox": "Fanbox",
    "Weibo": "微博",
    "Douyin": "抖音",
    "Xiaohongshu": "小红书",
    "Miyoushe": "米游社",
    "Lofter": "LOFTER",
    "Pinterest": "Pinterest",
    "NicoNico": "nico nico seiga",
    "动画番剧": "Anime",
}


@dataclass
class LegacyBlock:
    text: str
    thumb: Optional[bytes] = None


def service_name(candidate: Candidate) -> str:
    if candidate.platform == "动画番剧":
        return "Anime"
    info = classify(candidate.url)
    return SERVICE_NAMES.get(info.name, info.name or candidate.platform or "web")


def info_text(candidate: Candidate) -> str:
    custom = candidate.extra.get("ascii_info")
    if custom:
        return str(custom)
    kind = classify(candidate.url).kind
    author = (candidate.author or "").strip().lstrip("@")
    title = first_line(candidate.title or "", 120)
    date = (candidate.date or "")[:10]

    if candidate.platform == "动画番剧":
        return f"《{title}》" if title else "no info"
    if kind in ("x", "x_user"):
        if date:
            return f"「{date}」/「{author}」" if author else f"「{date}」"
        return f"「{title}」/「{author}」" if (title or author) else "no info"
    if kind in ("danbooru", "gelbooru"):
        if author or title:
            return f"[{author}]({title})" if author and title else (f"[{author}]" if author else f"[{title}]")
        return "no info"
    if kind in ("pixiv", "pixiv_user"):
        return f"「{title}」/「{author}」" if (title or author) else "no info"
    if title and author:
        return f"「{title}」/「{author}」"
    if title:
        return f"「{title}」"
    if author:
        return f"「{author}」"
    return "no info"


def similarity_value(candidate: Candidate) -> Optional[float]:
    # Yandex 收录页 / Ascii2D 本身没有相似度，不能编一个 100% 出来
    if candidate.engine in ("Yandex", "Ascii2D"):
        return None
    if candidate.similarity > 0:
        return float(candidate.similarity)
    if candidate.verified or candidate.trusted:
        return 100.0
    return None


def result_text(candidate: Candidate) -> str:
    service = service_name(candidate)
    similarity = similarity_value(candidate)
    suffix = f" 相似度:{similarity:.2f}%" if similarity is not None else ""
    return f"[{service}][{candidate.url}]{suffix}\n{info_text(candidate)}"


def group_candidates(
    candidates: Sequence[Candidate],
    per_engine: int = 2,
    *,
    include_mismatch: bool = False,
) -> List[Tuple[str, List[Candidate]]]:
    buckets = {engine: [] for engine in ENGINE_ORDER}
    for candidate in candidates:
        engine = candidate.engine
        if engine not in buckets or not candidate.url:
            continue
        if is_blocked_url(candidate.url) or is_listing_url(candidate.url):
            continue
        if candidate.extra.get("verify") == "mismatch":
            # 指纹核对不一致：默认不当作出处；整轮都没结果时才降级展示
            if not include_mismatch:
                continue
            candidate.extra["similar_only"] = True
        if any(item.url == candidate.url for item in buckets[engine]):
            continue
        buckets[engine].append(candidate)

    for engine, items in buckets.items():
        limit = max(1, per_engine) * 2 if engine == "Ascii2D" else max(1, per_engine)
        items.sort(
            key=lambda item: (item.verified, item.trusted, item.exact, item.similarity),
            reverse=True,
        )
        buckets[engine] = items[:limit]
    return [(engine, buckets[engine]) for engine in ENGINE_ORDER if buckets[engine]]


def decorate_thumb(data: bytes) -> Optional[bytes]:
    """原版 ats_pic：随机改四角像素，降低被平台按图 hash 拦截的概率。"""
    try:
        image = Image.open(io.BytesIO(data)).convert("RGB")
        image.thumbnail((360, 360), Image.Resampling.LANCZOS)
        width, height = image.size
        for position in ((0, 0), (0, height - 1), (width - 1, 0), (width - 1, height - 1)):
            image.putpixel(position, (random.randint(0, 255), random.randint(0, 255), random.randint(0, 255)))
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return buffer.getvalue()
    except Exception:
        return None


async def _fetch_thumb(client, candidate: Candidate) -> Optional[bytes]:
    url = candidate.thumb or candidate.image
    if not url:
        return None
    data = await client.get_bytes(
        url,
        max_bytes=4 * 1024 * 1024,
        timeout=10,
        headers={"Referer": candidate.url},
    )
    if not data:
        return None
    return decorate_thumb(data)


def _engine_error_hint(errors: dict, engine: str) -> str:
    error = str((errors or {}).get(engine, "") or "")
    lowered = error.lower()
    if "人机" in error or "captcha" in lowered or "unusual traffic" in lowered:
        return "被人机验证"
    if "playwright" in lowered or "未安装" in error or "没装" in error:
        return "没装浏览器"
    if "超时" in error or "timeout" in lowered:
        return "超时"
    if "js" in lowered or "浏览器" in error:
        return "需要浏览器"
    return "检索失败"


async def build_legacy_blocks(
    candidates: Sequence[Candidate],
    client,
    *,
    thumb_on: bool = True,
    per_engine: int = 2,
    engine_errors: Optional[dict] = None,
) -> List[LegacyBlock]:
    groups = group_candidates(candidates, per_engine=per_engine)
    relaxed = False
    if not groups and candidates:
        # 一个精确出处都没有，但引擎确实返回了图：宁可按「相似」给出，也别回一句失败
        groups = group_candidates(candidates, per_engine=per_engine, include_mismatch=True)
        relaxed = bool(groups)
    if not groups:
        blocks: List[LegacyBlock] = []
        if (engine_errors or {}).get("Google Lens"):
            blocks.append(
                LegacyBlock(
                    f"Google Lens 检索失败喵（{_engine_error_hint(engine_errors, 'Google Lens')}），换用其他引擎检索中…"
                )
            )
        if (engine_errors or {}).get("Ascii2D"):
            blocks.append(
                LegacyBlock(
                    f"Ascii2D 检索失败喵（{_engine_error_hint(engine_errors, 'Ascii2D')}），换用其他引擎检索中…"
                )
            )
        blocks.append(LegacyBlock(FAILURE_TEXT))
        return blocks

    blocks = []
    if relaxed:
        blocks.append(LegacyBlock("没找到完全一致的图喵，下面只是相似图："))
    present = {engine for engine, _ in groups}
    if "Google Lens" not in present and (engine_errors or {}).get("Google Lens"):
        blocks.append(
            LegacyBlock(
                f"Google Lens 检索失败喵（{_engine_error_hint(engine_errors, 'Google Lens')}），换用其他引擎检索中…"
            )
        )
    if "Ascii2D" not in present and (engine_errors or {}).get("Ascii2D"):
        blocks.append(
            LegacyBlock(f"Ascii2D 检索失败喵（{_engine_error_hint(engine_errors, 'Ascii2D')}），换用其他引擎检索中…")
        )

    flat = [candidate for _, items in groups for candidate in items]
    if thumb_on:
        thumbs = await asyncio.gather(*(_fetch_thumb(client, candidate) for candidate in flat))
    else:
        thumbs = [None] * len(flat)
    thumb_map = {id(candidate): thumb for candidate, thumb in zip(flat, thumbs)}

    for engine, items in groups:
        if engine == "Ascii2D":
            for mode, header in (("color", "——色调检索——"), ("bovw", "——特征检索——")):
                subset = [c for c in items if c.extra.get("ascii_mode", "color") == mode][: max(1, per_engine)]
                if not subset:
                    continue
                blocks.append(LegacyBlock(header))
                for candidate in subset:
                    thumb = thumb_map.get(id(candidate))
                    prefix = "[预览图下载失败]\n" if thumb_on and not thumb else ""
                    blocks.append(LegacyBlock(prefix + result_text(candidate), thumb))
            continue

        blocks.append(LegacyBlock(ENGINE_HEADERS[engine]))
        for candidate in items:
            thumb = thumb_map.get(id(candidate))
            prefix = "[预览图下载失败]\n" if thumb_on and not thumb else ""
            blocks.append(LegacyBlock(prefix + result_text(candidate), thumb))
    return blocks
