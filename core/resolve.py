"""把「候选链接」变成「可核对的原帖」。

两步：
1. 用平台公开接口/OG 元数据补齐作者、日期、原图直链；
2. 下载候选原图，与用户图做 dHash 比对，确认是不是同一张图。

第二步是准确率的核心：它能把「二传/相似图」从「精确出处」里剔出去。
"""

from __future__ import annotations

import asyncio
import html as html_lib
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Iterable, List

try:
    from astrbot.api import logger  # type: ignore
except Exception:  # pragma: no cover
    logger = logging.getLogger("astrbot_plugin_picfinder.resolve")

from .image import ImageInput, compare_images
from .platform import classify, first_line

# Candidate 在 engines.base 中；插件内用相对导入，单测时用绝对导入
try:
    from ..engines.base import Candidate  # type: ignore
except (ImportError, ValueError):  # pragma: no cover
    from engines.base import Candidate  # type: ignore


_MAX_META_BYTES = 700 * 1024
_VERIFY_BYTES = 7 * 1024 * 1024


def format_date(value: str) -> str:
    if not value:
        return ""
    text = str(value).strip()
    # 2026-11-24T04:21:02+09:00 / 2026-11-24 04:21:02
    match = re.match(r"(\d{4})-(\d{2})-(\d{2})", text)
    if match:
        return "-".join(match.groups())
    # Tue Nov 24 04:21:02 +0000 2026
    try:
        parsed = datetime.strptime(text, "%a %b %d %H:%M:%S %z %Y")
        return parsed.date().isoformat()
    except ValueError:
        pass
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc).date().isoformat()
    except ValueError:
        return text[:10]


def parse_fxtwitter(payload: dict) -> dict:
    tweet = (payload or {}).get("tweet") or {}
    author = tweet.get("author") or {}
    media = ((tweet.get("media") or {}).get("all")) or []
    images = [item.get("url") for item in media if item.get("url")]
    first_media = media[0] if media else {}
    text = tweet.get("text") or (tweet.get("raw_text") or {}).get("text") or ""
    return {
        "url": tweet.get("url") or "",
        "author": author.get("screen_name") or author.get("name") or "",
        "author_url": author.get("url") or "",
        "date": format_date(tweet.get("created_at") or ""),
        "title": first_line(text, 120),
        "image": first_media.get("url") or first_media.get("thumbnail_url") or "",
        "thumb": first_media.get("thumbnail_url") or "",
        "images": images[:6],
    }


def parse_pixiv(payload: dict) -> dict:
    body = (payload or {}).get("body") or {}
    if not body or payload.get("error"):
        return {}
    urls = body.get("urls") or {}
    user_id = body.get("userId") or ""
    original = urls.get("original") or ""
    images = [original] if original else []
    if original and body.get("pageCount"):
        for index in range(1, min(int(body["pageCount"]), 6)):
            images.append(re.sub(r"_p\d+\.", f"_p{index}.", original))
    return {
        "url": f"https://www.pixiv.net/artworks/{body.get('illustId')}" if body.get("illustId") else "",
        "author": body.get("userName") or "",
        "author_url": f"https://www.pixiv.net/users/{user_id}" if user_id else "",
        "date": format_date(body.get("createDate") or ""),
        "title": first_line(body.get("illustTitle") or body.get("title") or "", 120),
        "image": original or urls.get("regular") or "",
        "thumb": urls.get("thumb") or urls.get("small") or "",
        "images": images,
    }


def parse_danbooru(payload: dict) -> dict:
    if not isinstance(payload, dict) or payload.get("success") is False:
        return {}
    artists = (payload.get("tag_string_artist") or "").split()
    source = payload.get("source") or ""
    return {
        "url": f"https://danbooru.donmai.us/posts/{payload.get('id')}" if payload.get("id") else "",
        "author": " ".join(artists[:3]).replace("_", " "),
        "date": format_date(payload.get("created_at") or ""),
        "title": first_line(payload.get("tag_string_character") or "", 120),
        "image": payload.get("file_url") or payload.get("large_file_url") or "",
        "thumb": payload.get("preview_file_url") or "",
        "source_url": source if source.startswith("http") else "",
    }


def parse_bilibili(payload: dict) -> dict:
    if not isinstance(payload, dict) or payload.get("code") != 0:
        return {}
    data = payload.get("data") or {}
    owner = data.get("owner") or {}
    mid = owner.get("mid")
    pubdate = data.get("pubdate")
    date = ""
    if isinstance(pubdate, (int, float)) and pubdate > 0:
        date = datetime.fromtimestamp(pubdate, timezone(timedelta(hours=8))).date().isoformat()
    pic = data.get("pic") or ""
    if pic.startswith("http://"):
        pic = "https://" + pic[7:]
    return {
        "url": f"https://www.bilibili.com/video/{data.get('bvid')}" if data.get("bvid") else "",
        "author": owner.get("name") or "",
        "author_url": f"https://space.bilibili.com/{mid}" if mid else "",
        "date": date,
        "title": first_line(data.get("title") or "", 120),
        "image": pic,
        "thumb": pic,
    }


def parse_oembed(payload: dict) -> dict:
    return {
        "author": payload.get("author_name") or "",
        "author_url": payload.get("author_url") or "",
        "title": first_line(payload.get("title") or "", 120),
        "image": payload.get("thumbnail_url") or "",
        "thumb": payload.get("thumbnail_url") or "",
    }


def parse_open_graph(page: str) -> dict:
    metas = {}
    for tag in re.findall(r"<meta\b[^>]*>", page or "", re.I):
        attrs = {k.lower(): v for k, v in re.findall(r'([\w:.-]+)\s*=\s*"([^"]*)"', tag)}
        key = (attrs.get("property") or attrs.get("name") or "").lower()
        content = attrs.get("content")
        if key and content and key not in metas:
            metas[key] = html_lib.unescape(content)

    title = metas.get("og:title") or metas.get("twitter:title") or ""
    if not title:
        match = re.search(r"<title[^>]*>(.*?)</title>", page or "", re.I | re.S)
        title = html_lib.unescape(match.group(1)) if match else ""
    return {
        "title": first_line(title, 120),
        "image": metas.get("og:image") or metas.get("twitter:image") or "",
        "thumb": metas.get("og:image") or metas.get("twitter:image") or "",
        "author": metas.get("author") or metas.get("og:article:author") or "",
        "date": format_date(
            metas.get("article:published_time") or metas.get("og:published_time") or metas.get("datepublished") or ""
        ),
        "site": metas.get("og:site_name") or "",
    }


def _apply(candidate: Candidate, meta: dict) -> None:
    if not meta:
        return
    force = bool(meta.get("_authoritative"))
    for field in ("url", "title", "author", "author_url", "date"):
        value = (meta.get(field) or "").strip()
        if value and (force or not getattr(candidate, field, "")):
            setattr(candidate, field, value)
    thumb = (meta.get("thumb") or "").strip()
    if thumb and (force or not candidate.thumb):
        candidate.thumb = thumb
    image = (meta.get("image") or "").strip()
    if image and (force or not candidate.image):
        if candidate.image and candidate.image != image:
            candidate.extra.setdefault("alt_image", candidate.image)
        candidate.image = image
    elif image and candidate.image and image != candidate.image:
        candidate.extra.setdefault("alt_image", image)
    extra_images = [u for u in (meta.get("images") or []) if u and u != candidate.image]
    if extra_images:
        candidate.extra.setdefault("alt_images", extra_images[:6])
    if meta.get("source_url") and not candidate.extra.get("declared_source"):
        candidate.extra["declared_source"] = meta["source_url"]


class SourceResolver:
    """平台元数据补齐 + 原图指纹核对。"""

    def __init__(self, client, *, top_n: int = 8, verify: bool = True, timeout: float = 12.0):
        self.client = client
        self.top_n = top_n
        self.verify = verify
        self.timeout = timeout
        self._bili_ready = False

    async def enrich(self, candidates: Iterable[Candidate], img: ImageInput) -> List[Candidate]:
        items = list(candidates)
        # 先按平台权重挑值得解析的，避免对 30 条 Yandex 二传站逐一发请求
        ordered = sorted(items, key=lambda c: (classify(c.url).weight, c.similarity), reverse=True)
        targets = ordered[: self.top_n]

        semaphore = asyncio.Semaphore(5)

        async def run(candidate: Candidate) -> None:
            async with semaphore:
                try:
                    meta = await asyncio.wait_for(self._metadata(candidate), timeout=self.timeout + 4)
                    _apply(candidate, meta)
                except Exception as exc:
                    logger.debug(f"picfinder: 元数据解析跳过 {candidate.url}: {exc}")

        await asyncio.gather(*(run(c) for c in targets))

        if self.verify:
            verifiable = [c for c in targets if c.image][: self.top_n]
            verify_sem = asyncio.Semaphore(6)

            async def verify_one(candidate: Candidate) -> None:
                async with verify_sem:
                    try:
                        await asyncio.wait_for(self._verify(candidate, img), timeout=min(self.timeout, 10.0))
                    except Exception as exc:
                        logger.debug(f"picfinder: 指纹核对跳过 {candidate.image}: {exc}")

            await asyncio.gather(*(verify_one(c) for c in verifiable))
        return items

    # ------------------------------------------------------------------
    async def _metadata(self, candidate: Candidate) -> dict:
        info = classify(candidate.url)
        if info.kind == "x":
            meta = await self._x(candidate)
            meta["_authoritative"] = True
            return meta
        if info.kind == "pixiv":
            meta = await self._pixiv(candidate, info.item_id)
            meta["_authoritative"] = True
            return meta
        if info.kind == "danbooru":
            meta = await self._danbooru(candidate, info.item_id)
            meta["_authoritative"] = True
            return meta
        if info.kind == "youtube":
            meta = await self._youtube(candidate, info.item_id)
            meta["_authoritative"] = True
            return meta
        if info.kind == "bilibili" and info.item_id.startswith("BV"):
            meta = await self._bilibili(candidate, info.item_id)
            meta["_authoritative"] = True
            return meta
        if info.kind in ("pinterest",):
            return {}
        return await self._open_graph(candidate)

    async def _x(self, candidate: Candidate) -> dict:
        match = re.search(r"/status/(\d+)", candidate.url)
        if not match:
            return {}
        resp = await self.client.get(
            f"https://api.fxtwitter.com/status/{match.group(1)}",
            timeout=self.timeout,
        )
        if resp.status != 200:
            return {}
        return parse_fxtwitter(resp.json())

    async def _pixiv(self, candidate: Candidate, illust_id: str) -> dict:
        if not illust_id:
            return {}
        resp = await self.client.get(
            f"https://www.pixiv.net/ajax/illust/{illust_id}",
            headers={"Referer": "https://www.pixiv.net/"},
            timeout=self.timeout,
        )
        if resp.status != 200:
            return {}
        return parse_pixiv(resp.json())

    async def _danbooru(self, candidate: Candidate, post_id: str) -> dict:
        if not post_id:
            return {}
        resp = await self.client.get(
            f"https://danbooru.donmai.us/posts/{post_id}.json",
            timeout=self.timeout,
        )
        if resp.status != 200:
            return {}
        return parse_danbooru(resp.json())

    async def _bilibili(self, candidate: Candidate, bvid: str) -> dict:
        if not bvid:
            return {}
        if not self._bili_ready:
            try:
                await self.client.get("https://www.bilibili.com/", timeout=self.timeout)
            except Exception:
                pass
            self._bili_ready = True
        resp = await self.client.get(
            "https://api.bilibili.com/x/web-interface/view",
            params={"bvid": bvid},
            headers={"Referer": "https://www.bilibili.com/"},
            timeout=self.timeout,
        )
        if resp.status != 200:
            return {}
        return parse_bilibili(resp.json())

    async def _youtube(self, candidate: Candidate, video_id: str) -> dict:
        if not video_id:
            return {}
        resp = await self.client.get(
            "https://www.youtube.com/oembed",
            params={"url": f"https://www.youtube.com/watch?v={video_id}", "format": "json"},
            timeout=self.timeout,
        )
        if resp.status != 200:
            return {}
        return parse_oembed(resp.json())

    async def _open_graph(self, candidate: Candidate) -> dict:
        resp = await self.client.get(
            candidate.url,
            timeout=self.timeout,
            browser_headers=True,
            headers={"Referer": "https://www.google.com/"},
        )
        if resp.status != 200:
            return {}
        return parse_open_graph(resp.text[:_MAX_META_BYTES])

    # ------------------------------------------------------------------
    async def _verify(self, candidate: Candidate, img: ImageInput) -> None:
        if not img.has_hash:
            return
        # 视频来源的封面通常不是用户截的那一帧，核对会误伤，直接跳过
        if classify(candidate.url).kind in ("bilibili", "youtube", "niconico"):
            return
        urls = []
        for url in [candidate.image, *(candidate.extra.get("alt_images") or []), candidate.extra.get("alt_image")]:
            if url and url not in urls:
                urls.append(url)
        if not urls:
            return

        best_match = None
        best_url = ""
        for url in urls[:3]:
            data = await self.client.get_bytes(
                url,
                max_bytes=_VERIFY_BYTES,
                timeout=min(self.timeout, 4.0),
                headers={"Referer": candidate.url},
            )
            if not data:
                continue
            try:
                other = ImageInput.create(data, source_url=url)
            except ValueError:
                continue
            match = compare_images(img, other)
            rank = (2 if match.exact else 1 if match.near else 0, -match.distance)
            if best_match is None or rank > (
                2 if best_match.exact else 1 if best_match.near else 0,
                -best_match.distance,
            ):
                best_match, best_url = match, url
            if match.exact:
                break

        if best_match is None:
            candidate.extra["verify"] = "download_failed"
            return

        was_trusted = candidate.trusted
        candidate.extra["hash_distance"] = best_match.distance
        candidate.extra["verified_image"] = best_url
        if best_match.kind:
            candidate.extra["match"] = best_match.kind

        if best_match.exact:
            candidate.verified = True
            candidate.exact = True
            candidate.image = best_url
        elif best_match.near:
            candidate.extra["verify"] = "near"
        else:
            candidate.extra["verify"] = "mismatch_trusted" if was_trusted else "mismatch"
            if not was_trusted:
                candidate.exact = False
