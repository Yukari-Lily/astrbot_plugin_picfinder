"""Yandex 以图搜图引擎（精确来源列表）。

Yandex 的「站点」结果就是「哪些网页收录了这张图」，而且是服务端渲染，
在 Google Lens 被风控/需要浏览器时，是稳定、可复核的精确出处来源。
"""

from __future__ import annotations

import html as html_lib
import json
import logging
import re
import time
from typing import List, Optional

try:
    from astrbot.api import logger  # type: ignore
except Exception:  # pragma: no cover
    logger = logging.getLogger("astrbot_plugin_picfinder.yandex")

try:
    from ..core.image import ImageInput  # type: ignore
except (ImportError, ValueError):  # pragma: no cover
    from core.image import ImageInput  # type: ignore
try:
    from ..core.platform import classify, clean_url, first_line, is_blocked_url  # type: ignore
except (ImportError, ValueError):  # pragma: no cover
    from core.platform import classify, clean_url, first_line, is_blocked_url  # type: ignore
from .base import Candidate, EngineResult


def _upload_parts(img: ImageInput, *, max_bytes: int = 3 * 1024 * 1024, max_side: int = 3000):
    data, mime, filename = img.upload_parts(max_bytes=max_bytes, max_side=max_side)
    return filename, data, mime or "image/jpeg"


_UPLOAD_URL = "https://yandex.com/images/search"
_STATE_PATTERN = re.compile(r'data-state="([^"]*)"')


class YandexError(RuntimeError):
    pass


def extract_cbir_state(page_html: str) -> Optional[dict]:
    """从 Yandex 结果页的 data-state 中取出 cbirSites。"""
    if not page_html:
        return None
    for match in _STATE_PATTERN.finditer(page_html):
        raw = html_lib.unescape(match.group(1))
        if "cbirSites" not in raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        state = (data or {}).get("initialState") or {}
        cbir = state.get("cbirSites") or {}
        if cbir.get("sites"):
            return cbir
    # 某些版本把 JSON 放在脚本文本里
    index = page_html.find('"cbirSites"')
    if index < 0:
        return None
    start = page_html.rfind("{", 0, index)
    if start < 0:
        return None
    try:
        data, _ = json.JSONDecoder().raw_decode(html_lib.unescape(page_html[start:]))
    except Exception:
        return None
    return ((data or {}).get("initialState") or {}).get("cbirSites")


def parse_cbir_sites(cbir: Optional[dict], limit: int = 12) -> List[Candidate]:
    if not cbir:
        return []
    sites = cbir.get("sites") or []
    candidates: List[Candidate] = []
    seen_urls = set()
    seen_images = set()

    for site in sites:
        if not isinstance(site, dict):
            continue
        url = clean_url(site.get("url", "") or "")
        if not url or is_blocked_url(url) or url in seen_urls:
            continue
        original = site.get("originalImage") or {}
        image = original.get("url") or ""
        thumb = ((site.get("thumb") or {}).get("url")) or ""
        if thumb.startswith("//"):
            thumb = "https:" + thumb
        key = image or thumb
        if key:
            normalized = re.sub(r"/(?:\d+x\d+|orig)/", "/", key)
            if normalized in seen_images:
                continue
            seen_images.add(normalized)

        info = classify(url)
        seen_urls.add(url)
        candidates.append(
            Candidate(
                url=info.canonical or url,
                title=first_line(site.get("title", "") or "", 120),
                platform=info.name,
                date="",
                image=image,
                thumb=thumb,
                engine="Yandex",
                exact=True,
                extra={
                    "description": first_line(site.get("description", "") or "", 160),
                    "domain": site.get("domain", ""),
                    "size": f"{original.get('width')}x{original.get('height')}" if original.get("width") else "",
                },
            )
        )
        if len(candidates) >= limit:
            break
    return candidates


class YandexEngine:
    engine_name = "Yandex"

    def __init__(self, client, *, family_filter: bool = False, timeout: float = 35.0):
        self.client = client
        self.family_filter = family_filter
        self.timeout = timeout

    async def search(self, img: ImageInput, limit: int = 12) -> EngineResult:
        started = time.monotonic()
        result = EngineResult(engine=self.engine_name)
        try:
            cbir_id = await self._upload(img)
            payload = await self._sites(cbir_id)
            result.candidates = parse_cbir_sites(payload, limit=limit)
            if not result.candidates:
                result.error = "无来源列表"
        except Exception as exc:
            result.error = str(exc)[:200]
        result.elapsed = time.monotonic() - started
        return result

    async def _upload(self, img: ImageInput) -> str:
        request = json.dumps({"blocks": [{"block": "b-page_type_search-by-image__link"}]})
        params = {"rpt": "imageview", "format": "json", "request": request}
        if not self.family_filter:
            params["family"] = "no"

        # Yandex 的入口对体积很敏感：几 MB 的 PNG 会 413/502。
        # 先按 3MB 压，失败再压到 900KB 重试一次。
        attempts = ((3 * 1024 * 1024, 3000), (900 * 1024, 1600))
        last_error = ""
        for max_bytes, max_side in attempts:
            filename, data, mime = _upload_parts(img, max_bytes=max_bytes, max_side=max_side)
            try:
                resp = await self.client.post(
                    _UPLOAD_URL,
                    params=params,
                    files={"upfile": (filename, data, mime)},
                    data={"prg": "1"},
                    timeout=self.timeout,
                    browser_headers=True,
                )
            except Exception as exc:
                # 隧道偶发 RST / 连接被掐：压小一点再试，别整条引擎直接失败
                last_error = f"上传异常: {str(exc)[:120]}"
                logger.debug(f"picfinder: Yandex {last_error}")
                continue
            if resp.status in (413, 502, 503):
                last_error = f"上传 HTTP {resp.status}（图片 {len(data) // 1024}KB 被拒）"
                logger.debug(f"picfinder: Yandex {last_error}，压缩后重试")
                continue
            if resp.status != 200:
                raise YandexError(f"上传 HTTP {resp.status}")
            cbir_id = self._extract_cbir_id(resp)
            if cbir_id:
                return cbir_id
            last_error = "上传响应解析失败"
            break
        raise YandexError(last_error or "上传失败")

    @staticmethod
    def _extract_cbir_id(resp) -> str:
        # 多数情况下返回 JSON；被当作普通页面渲染时，cbir_id 会出现在跳转地址里
        cbir_id = ""
        try:
            data = resp.json()
            cbir_id = str(data["blocks"][0]["params"]["cbirId"] or "")
        except Exception:
            cbir_id = ""
        if not cbir_id:
            from urllib.parse import parse_qs, unquote, urlsplit

            query = parse_qs(urlsplit(resp.url).query)
            cbir_id = unquote((query.get("cbir_id") or [""])[0])
        if not cbir_id:
            match = re.search(r"cbir_id=([0-9A-Za-z_%\-]+)", resp.text)
            if match:
                from urllib.parse import unquote

                cbir_id = unquote(match.group(1))
        return cbir_id

    async def _sites(self, cbir_id: str) -> Optional[dict]:
        params = {"rpt": "imageview", "cbir_page": "sites", "cbir_id": cbir_id}
        if not self.family_filter:
            params["family"] = "no"
        resp = await self.client.get(_UPLOAD_URL, params=params, timeout=self.timeout, browser_headers=True)
        if resp.status != 200:
            raise YandexError(f"结果页 HTTP {resp.status}")
        state = extract_cbir_state(resp.text)
        if not state:
            if "/showcaptcha" in str(resp.url) or "captcha" in resp.text[:5000].lower():
                raise YandexError("Yandex 要求人机验证")
            raise YandexError("结果页结构异常")
        return state
