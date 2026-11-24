"""SauceNAO 引擎（二次元/Pixiv 精确图库）。

有 API Key 时走官方 JSON 接口；没有 Key 时走网页表单并解析结果，
不再依赖 PicImageSearch。
"""

from __future__ import annotations

import html as html_lib
import logging
import re
import time
from typing import List

try:
    from astrbot.api import logger  # type: ignore
except Exception:  # pragma: no cover
    logger = logging.getLogger("astrbot_plugin_picfinder.saucenao")

try:
    from ..core.image import ImageInput  # type: ignore
except (ImportError, ValueError):  # pragma: no cover
    from core.image import ImageInput  # type: ignore
try:
    from ..core.platform import classify, clean_url, first_line  # type: ignore
except (ImportError, ValueError):  # pragma: no cover
    from core.platform import classify, clean_url, first_line  # type: ignore
from .base import Candidate, EngineResult


def _upload_parts(img: ImageInput):
    data, mime, filename = img.upload_parts()
    return filename, data, mime or "image/jpeg"


_ENDPOINT = "https://saucenao.com/search.php"
_TAG_RE = re.compile(r"<[^>]+>")
_SIM_RE = re.compile(r"resultsimilarityinfo[^>]*>\s*([\d.]+)%")
_INDEX_RE = re.compile(r"Index #(\d+):")
_TITLE_RE = re.compile(r'<div class="resulttitle">\s*(?:<strong>)?(.*?)(?:</strong>)?\s*</div>', re.S)
_AUTHOR_RE = re.compile(r"Author:\s*</strong>\s*<a[^>]*>(.*?)</a>", re.S)
_HREF_RE = re.compile(r'href="([^"]+)"')


def _clean_html(text: str) -> str:
    return first_line(html_lib.unescape(_TAG_RE.sub(" ", text or "")), 140)


def _pick_source(urls: List[str]) -> str:
    """优先选带作品 ID 的链接（如 status/artworks/posts），而不是作者主页。"""
    if not urls:
        return ""
    return max(urls, key=lambda url: (1 if classify(url).item_id else 0, classify(url).weight))


def parse_saucenao_json(payload: dict, threshold: float = 65.0) -> List[Candidate]:
    candidates: List[Candidate] = []
    for entry in (payload or {}).get("results") or []:
        header = entry.get("header") or {}
        data = entry.get("data") or {}
        try:
            similarity = float(str(header.get("similarity", "0")))
        except ValueError:
            similarity = 0.0
        if similarity < 35:
            continue

        urls = [str(u) for u in (data.get("ext_urls") or [])]
        url = clean_url(_pick_source(urls)) if urls else ""
        if not url and data.get("source", "").startswith("http"):
            url = clean_url(data["source"])
        if not url:
            continue

        title = data.get("title") or data.get("jp_name") or ""
        if not title and data.get("source"):
            title = str(data["source"])
        author = (
            data.get("member_name")
            or data.get("author_name")
            or data.get("creator")
            or data.get("twitter_user_handle")
            or ""
        )
        info = classify(url)
        candidates.append(
            Candidate(
                url=info.canonical or url,
                title=first_line(str(title), 120),
                platform=info.name,
                author=str(author),
                thumb=header.get("thumbnail") or "",
                engine="SauceNAO",
                exact=similarity >= threshold,
                similarity=similarity,
                extra={"index_id": header.get("index_id")},
            )
        )
    candidates.sort(key=lambda c: c.similarity, reverse=True)
    return candidates


def parse_saucenao_html(page_html: str, threshold: float = 65.0) -> List[Candidate]:
    if not page_html:
        return []
    blocks = re.split(r'<div class="result(?: hidden)?"', page_html)
    candidates: List[Candidate] = []
    for block in blocks[1:]:
        if "result-hidden-notification" in block[:200]:
            continue
        sim_match = _SIM_RE.search(block)
        if not sim_match:
            continue
        try:
            similarity = float(sim_match.group(1))
        except ValueError:
            continue
        if similarity < 35:
            continue

        title_match = _TITLE_RE.search(block)
        title = _clean_html(title_match.group(1)) if title_match else ""

        author = ""
        author_match = _AUTHOR_RE.search(block)
        if author_match:
            author = _clean_html(author_match.group(1))

        hrefs = [
            html_lib.unescape(href)
            for href in _HREF_RE.findall(block)
            if href.startswith(("http://", "https://")) and "saucenao.com" not in href and "/images/" not in href
        ]
        if not hrefs:
            continue
        url = clean_url(_pick_source(hrefs))
        info = classify(url)

        index_match = _INDEX_RE.search(block)
        thumb_match = re.search(r'<img[^>]+src="([^"]+)"', block)
        candidates.append(
            Candidate(
                url=info.canonical or url,
                title=title,
                platform=info.name,
                author=author,
                thumb=html_lib.unescape(thumb_match.group(1)) if thumb_match else "",
                engine="SauceNAO",
                exact=similarity >= threshold,
                similarity=similarity,
                extra={"index_id": int(index_match.group(1)) if index_match else None},
            )
        )
    candidates.sort(key=lambda c: c.similarity, reverse=True)
    return candidates


class SauceNAOEngine:
    engine_name = "SauceNAO"

    def __init__(self, client, *, api_key: str = "", threshold: float = 65.0, timeout: float = 30.0):
        self.client = client
        self.api_key = (api_key or "").strip()
        self.threshold = float(threshold)
        self.timeout = timeout

    async def search(self, img: ImageInput, limit: int = 8) -> EngineResult:
        started = time.monotonic()
        result = EngineResult(engine=self.engine_name)
        if self.api_key:
            try:
                payload = await self._search_api(img, limit)
                result.candidates = parse_saucenao_json(payload, self.threshold)
                if not result.candidates:
                    result.error = "无匹配结果"
                result.elapsed = time.monotonic() - started
                return result
            except Exception as exc:
                logger.debug(f"SauceNAO API 失败，回退网页: {exc}")

        try:
            page = await self._search_html(img, limit)
            result.candidates = parse_saucenao_html(page, self.threshold)
            if not result.candidates:
                result.error = "无匹配结果"
        except Exception as exc:
            result.error = str(exc)[:200]
        result.elapsed = time.monotonic() - started
        return result

    async def _search_api(self, img: ImageInput, limit: int) -> dict:
        data = {
            "output_type": "2",
            "numres": str(max(1, min(limit, 20))),
            "db": "999",
            "api_key": self.api_key,
        }
        resp = await self.client.post(
            _ENDPOINT,
            data=data,
            files={"file": (*_upload_parts(img),)},
            timeout=self.timeout,
        )
        if resp.status != 200:
            raise RuntimeError(f"HTTP {resp.status}")
        payload = resp.json()
        if payload.get("header", {}).get("status", 0) < 0:
            raise RuntimeError(payload.get("header", {}).get("message", "API 错误"))
        return payload

    async def _search_html(self, img: ImageInput, limit: int) -> str:
        data = {"db": "999", "numres": str(max(1, min(limit, 20))), "hide": "0"}
        resp = await self.client.post(
            _ENDPOINT,
            data=data,
            files={"file": (*_upload_parts(img),)},
            timeout=self.timeout,
            browser_headers=True,
        )
        if resp.status != 200:
            raise RuntimeError(f"HTTP {resp.status}")
        if "The anonymous account type does not permit API usage" in resp.text:
            raise RuntimeError("SauceNAO 拒绝匿名请求")
        return resp.text
