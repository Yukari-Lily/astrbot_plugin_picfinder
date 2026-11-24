"""Ascii2D 引擎（色调检索 + bovw 特征检索）。

带 Rails CSRF token 与会话 cookie；结果按原版格式输出：
    ——色调检索——
    [缩略图]
    [pixiv][链接]
    「标题」/「画师」
Ascii2D 没有相似度，所以不显示 `相似度:`。
"""

from __future__ import annotations

import html as html_lib
import re
import time
from typing import List, Tuple
from urllib.parse import urljoin

from .base import Candidate, EngineResult

try:
    from ..core.image import ImageInput  # type: ignore
    from ..core.platform import classify, first_line  # type: ignore
except (ImportError, ValueError):  # pragma: no cover
    from core.image import ImageInput  # type: ignore
    from core.platform import classify, first_line  # type: ignore


BASE_URL = "https://ascii2d.net"
_TOKEN_RE = re.compile(r'name="authenticity_token"\s+value="([^"]+)"')
_CHALLENGE_MARKERS = (
    "just a moment",
    "cf_chl_opt",
    "_cf_chl_",
    "attention required",
    "verifying you are human",
)
_BLOCK_RE = re.compile(r'<div[^>]+class="[^"]*row item-box[^"]*"', re.I)
_IMG_RE = re.compile(r'<img[^>]+(?:data-src|src)="([^"]+)"', re.I)
_LINK_RE = re.compile(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', re.I | re.S)
_BOVW_RE = re.compile(r'href="([^"]*?/search/bovw/[^"]+)"', re.I)
_TAG_RE = re.compile(r"<[^>]+>")


def _clean(fragment: str) -> str:
    text = html_lib.unescape(_TAG_RE.sub(" ", fragment or ""))
    return re.sub(r"\s+", " ", text).strip()


def parse_ascii_html(page: str, limit: int = 2, mode: str = "color") -> List[Candidate]:
    """解析 Ascii2D 结果页（row item-box 卡片）。"""
    if not page:
        return []
    blocks = _BLOCK_RE.split(page)[1:]
    candidates: List[Candidate] = []
    seen = set()

    for block in blocks:
        if len(candidates) >= limit:
            break
        links: List[Tuple[str, str]] = []
        for href, text in _LINK_RE.findall(block):
            href = html_lib.unescape(href).strip()
            if not href.startswith("http"):
                continue
            if "ascii2d.net" in href or "/search/" in href:
                continue
            links.append((href, _clean(text)))
        if not links:
            continue

        def score(item: Tuple[str, str]) -> Tuple[int, int]:
            info = classify(item[0])
            return (1 if info.item_id else 0, info.weight)

        url, _ = max(links, key=score)
        if url in seen:
            continue
        seen.add(url)

        info = classify(url)
        thumb_match = _IMG_RE.search(block)
        thumb = urljoin(BASE_URL, html_lib.unescape(thumb_match.group(1))) if thumb_match else ""

        texts = [text for _, text in links if text]
        if info.kind in ("pixiv", "x") and len(texts) >= 2:
            detail = f"「{texts[0]}」/「{texts[1].lstrip('@')}」"
        elif texts:
            detail = f"「{texts[0]}」"
        else:
            plain = _clean(block)
            detail = f"「{first_line(plain, 80)}」" if plain else "no info"

        candidates.append(
            Candidate(
                url=info.canonical or url,
                title=first_line(texts[0] if texts else "", 120),
                platform=info.name,
                author=texts[1].lstrip("@") if len(texts) >= 2 else info.author_hint,
                thumb=thumb,
                engine="Ascii2D",
                exact=False,
                similarity=0.0,
                extra={"ascii_info": detail, "ascii_mode": mode},
            )
        )
    return candidates


class Ascii2DEngine:
    engine_name = "Ascii2D"

    def __init__(
        self,
        client,
        *,
        bovw: bool = True,
        timeout: float = 30.0,
        browser=None,
        browser_error: str = "",
    ):
        self.client = client
        self.bovw = bovw
        self.timeout = timeout
        self.browser = browser
        self.browser_error = browser_error

    async def search(self, img: ImageInput, limit: int = 2) -> EngineResult:
        started = time.monotonic()
        result = EngineResult(engine=self.engine_name)
        try:
            token = await self._token()
            page, page_url = await self._submit_file(token, img)
            candidates = parse_ascii_html(page, limit=limit, mode="color")
            if self.bovw and len(candidates) < limit:
                bovw_url = self._bovw_url(page, page_url)
                if bovw_url:
                    resp = await self.client.get(
                        bovw_url,
                        timeout=self.timeout,
                        browser_headers=True,
                        headers={"Referer": page_url},
                    )
                    if resp.status == 200:
                        candidates.extend(parse_ascii_html(resp.text, limit=limit - len(candidates), mode="bovw"))
            result.candidates = candidates
            if not candidates:
                result.error = "无结果"
        except Exception as exc:
            # 403/Cloudflare 时尝试真浏览器；浏览器也过不去就返回可读原因
            try:
                page, _ = await self._browser_search(img)
                candidates = parse_ascii_html(page, limit=limit, mode="color")
                result.candidates = candidates
                if not candidates:
                    result.error = f"{exc}；浏览器未解析到结果"
            except Exception as browser_exc:
                result.error = f"{exc}；浏览器: {browser_exc}"[:200]
        result.elapsed = time.monotonic() - started
        return result

    async def _token(self) -> str:
        resp = await self.client.get(BASE_URL + "/", timeout=self.timeout, browser_headers=True)
        if resp.status != 200:
            raise RuntimeError(f"主页 HTTP {resp.status}")
        if any(marker in resp.text.lower() for marker in _CHALLENGE_MARKERS):
            raise RuntimeError("Ascii2D 被人机验证（需要 Playwright 浏览器过盾）")
        match = _TOKEN_RE.search(resp.text)
        if not match:
            raise RuntimeError("Ascii2D 未找到 authenticity_token")
        return match.group(1)

    async def _submit_file(self, token: str, img: ImageInput) -> Tuple[str, str]:
        data = {"utf8": "✓", "authenticity_token": token, "search": ""}
        files = {"file": (img.filename, img.payload(max_bytes=4 * 1024 * 1024, max_side=2048), img.mime or "image/jpeg")}
        headers = {"Origin": BASE_URL, "Referer": BASE_URL + "/"}
        resp = await self.client.post(
            BASE_URL + "/search/file",
            data=data,
            files=files,
            headers=headers,
            timeout=self.timeout,
            browser_headers=True,
        )
        text = resp.text or ""
        if resp.status == 403 or any(marker in text.lower() for marker in _CHALLENGE_MARKERS):
            raise RuntimeError("Ascii2D 被人机验证（需要 Playwright 浏览器过盾）")
        if resp.status != 200:
            raise RuntimeError(f"Ascii2D 搜索 HTTP {resp.status}")
        return text, str(resp.url)

    async def _browser_search(self, img: ImageInput) -> Tuple[str, str]:
        if self.browser is None:
            raise RuntimeError(self.browser_error or "通用浏览器组件未安装")
        page = await self.browser.new_page()
        try:
            await page.goto(BASE_URL + "/", timeout_ms=int(self.timeout * 1000))
            try:
                await page.wait_for_selector("#file-form input[type=file]", timeout_ms=20000)
            except Exception:
                html = await page.content()
                if any(marker in html.lower() for marker in _CHALLENGE_MARKERS):
                    raise RuntimeError("被 Cloudflare 拦住") from None
                raise RuntimeError("上传表单没加载出来") from None
            await page.set_input_files(
                "#file-form input[type=file]",
                files=[
                    {
                        "name": img.filename,
                        "mimeType": img.mime or "image/jpeg",
                        "buffer": img.payload(max_bytes=4 * 1024 * 1024, max_side=2048),
                    }
                ],
            )
            await page.click(selector="#file_upload button[type=submit]")

            # 提交后是重定向到 /search/<hash>：等地址变化，而不是等结果卡片
            page_url = ""
            for _ in range(30):
                await page.wait(500)
                page_url = await page.url()
                if "/search/" in page_url:
                    break
            if "/search/" not in page_url:
                html = await page.content()
                if any(marker in html.lower() for marker in _CHALLENGE_MARKERS):
                    raise RuntimeError("被 Cloudflare 拦住")
                raise RuntimeError("提交后没跳到结果页")

            try:
                await page.wait_for_selector(".row.item-box", timeout_ms=6000)
            except Exception:
                await page.wait(800)
            html = await page.content()
            if "row item-box" not in html and any(
                marker in html.lower() for marker in _CHALLENGE_MARKERS
            ):
                raise RuntimeError("被 Cloudflare 拦住")
            return html, page_url
        finally:
            await page.close()

    @staticmethod
    def _bovw_url(page: str, page_url: str) -> str:
        match = _BOVW_RE.search(page)
        if not match:
            return ""
        return urljoin(page_url, html_lib.unescape(match.group(1)))
