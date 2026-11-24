"""Google Lens 引擎。

2025 年后 Google 的 Lens 结果页改为纯前端渲染，直接抓 HTML 只能拿到
「enablejs」空壳。因此这里分成两条路径：

* ``browser``——主路径：在浏览器里直接 fetch 上传（带上浏览器自己的 Cookie
  与指纹），拿到 vsrid 结果地址后渲染「完全匹配」页，再从 DOM 取外部链接；
* ``http``   ——兜底路径：用 HTTP 客户端上传并解析服务端渲染页，只有少数
  地区/网络还会返回 SSR。

两条路径都只把「完全匹配」页当作精确出处；可选的「视觉匹配」会明确标记为相似，
不会冒充原图。
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from html import unescape
from html.parser import HTMLParser
from typing import Iterable, List, Optional
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlsplit, urlunsplit

try:
    from astrbot.api import logger  # type: ignore
except Exception:  # pragma: no cover
    logger = logging.getLogger("astrbot_plugin_picfinder.lens")

try:
    from ..core.image import ImageInput  # type: ignore
except (ImportError, ValueError):  # pragma: no cover
    from core.image import ImageInput  # type: ignore
try:
    from ..core.platform import classify, clean_url, first_line, is_blocked_url, normalize_space  # type: ignore
except (ImportError, ValueError):  # pragma: no cover
    from core.platform import classify, clean_url, first_line, is_blocked_url, normalize_space  # type: ignore
from .base import Candidate, EngineResult

LENS_UPLOAD = "https://lens.google.com/v3/upload"
LENS_HOME = "https://lens.google.com"
# 有公开图床地址时不必上传：GET 一枪就直接跳到 vsrid 结果页
LENS_BY_URL = "https://www.google.com/searchbyimage?hl={hl}&image_url={url}"

_UDM_EXACT = "48"
_UDM_VISUAL = "44"

_SHELL_MARKERS = (
    "enablejs",
    "Please click here if you are not redirected",
    "如果您在几秒钟内没有被重定向",
)


class LensError(RuntimeError):
    """Lens 不可用（风控/未安装浏览器等），携带可读原因。"""


class LensBlocked(LensError):
    pass


@dataclass
class LensPage:
    url: str = ""
    html: str = ""


def _strip_dom_noise(text: str) -> str:
    text = normalize_space(text)
    text = re.sub(r"^(?:https?://)?(?:www\.)?[\w.-]+\.[a-z]{2,}\s*", "", text, flags=re.I)
    return text.strip(" ·-—|")


def _looks_like_results(html: str) -> bool:
    if not html:
        return False
    if any(marker in html for marker in _SHELL_MARKERS):
        return False
    return "YxbOwd" in html or "vEWxFf" in html or "AF_initDataCallback" in html


def _with_udm(url: str, udm: str, safe_search: bool) -> str:
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k not in ("udm", "safe")]
    query.append(("udm", udm))
    query.append(("safe", "active" if safe_search else "off"))
    return urlunsplit((parts.scheme or "https", parts.netloc, parts.path, urlencode(query), ""))


# ---------------------------------------------------------------------------
# 服务端 HTML 解析
# ---------------------------------------------------------------------------
class _LensBlockParser(HTMLParser):
    """按 Google Lens 结果卡片（YxbOwd=完全匹配 / vEWxFf=视觉匹配）切块。"""

    CONTAINERS = ("YxbOwd", "vEWxFf")

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: List[dict] = []
        self._capture: Optional[dict] = None
        self._depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        attrs = {k: (v or "") for k, v in attrs}
        if self._capture is None:
            if tag == "div":
                classes = attrs.get("class", "").split()
                hit = next((c for c in self.CONTAINERS if c in classes), "")
                if hit:
                    self._capture = {"class": hit, "hrefs": [], "imgs": [], "text": []}
                    self._depth = 1
                    return
            return

        if tag == "div":
            self._depth += 1
        elif tag == "a":
            href = attrs.get("href", "")
            if href:
                self._capture["hrefs"].append(href)
        elif tag == "img":
            src = attrs.get("src") or attrs.get("data-src") or ""
            if src:
                self._capture["imgs"].append(src)
        elif tag in ("br", "p", "span", "div"):
            self._capture["text"].append(" ")

    def handle_endtag(self, tag: str) -> None:
        if self._capture is None:
            return
        if tag == "div":
            self._depth -= 1
            if self._depth <= 0:
                self.blocks.append(self._capture)
                self._capture = None

    def handle_data(self, data: str) -> None:
        if self._capture is not None:
            self._capture["text"].append(data)


class _AnchorParser(HTMLParser):
    """兜底：抓出页面中带文字/图片的外部链接。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.anchors: List[dict] = []
        self._current: Optional[dict] = None

    def handle_starttag(self, tag: str, attrs) -> None:
        attrs = {k: (v or "") for k, v in attrs}
        if tag == "a":
            if self._current is not None:
                self._finish()
            self._current = {"href": attrs.get("href", ""), "text": [], "imgs": []}
        elif tag == "img" and self._current is not None:
            src = attrs.get("src") or attrs.get("data-src") or ""
            if src:
                self._current["imgs"].append(src)

    def handle_data(self, data: str) -> None:
        if self._current is not None:
            self._current["text"].append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._current is not None:
            self._finish()

    def _finish(self) -> None:
        if self._current is not None:
            self.anchors.append(self._current)
            self._current = None


def parse_lens_html(html: str, limit: int = 8, exact: bool = True) -> List[Candidate]:
    """解析 Lens 服务端渲染页（或任何含结果卡片的 HTML）。"""
    if not html:
        return []
    parser = _LensBlockParser()
    try:
        parser.feed(html)
    except Exception:
        pass

    entries: List[dict] = []
    if parser.blocks:
        for block in parser.blocks:
            entries.append(
                {
                    "href": next((h for h in block["hrefs"] if h.startswith(("http", "//"))), ""),
                    "text": " ".join(block["text"]),
                    "imgs": block["imgs"],
                    "exact": block["class"] == "YxbOwd",
                }
            )
    else:
        anchor_parser = _AnchorParser()
        try:
            anchor_parser.feed(html)
        except Exception:
            pass
        for anchor in anchor_parser.anchors:
            entries.append(
                {
                    "href": anchor["href"],
                    "text": " ".join(anchor["text"]),
                    "imgs": anchor["imgs"],
                    "exact": exact,
                }
            )

    return _entries_to_candidates(entries, limit=limit, default_exact=exact)


def parse_lens_dom(payload: dict, limit: int = 8, exact: bool = True) -> List[Candidate]:
    """解析浏览器 evaluate 返回的 {items:[{href,text,img}]}。"""
    items = (payload or {}).get("items") or []
    entries = [
        {
            "href": item.get("href", ""),
            "text": item.get("text", ""),
            "imgs": [item.get("img", "")] if item.get("img") else [],
            "exact": exact,
        }
        for item in items
    ]
    return _entries_to_candidates(entries, limit=limit, default_exact=exact)


def _entries_to_candidates(entries: Iterable[dict], limit: int, default_exact: bool) -> List[Candidate]:
    candidates: List[Candidate] = []
    seen = set()
    for entry in entries:
        href = (entry.get("href") or "").strip()
        if href.startswith("//"):
            href = "https:" + href
        if not href:
            continue
        href = urljoin("https://www.google.com/", href)
        href = clean_url(href)
        if not href.startswith(("http://", "https://")) or is_blocked_url(href):
            continue
        # Google 自身的导航链接（结果里偶尔夹带 /search、/url 等）
        host = urlsplit(href).hostname or ""
        if host.endswith("google.com") or host.endswith("google.com.hk") or host.endswith("google.co.jp"):
            continue

        canonical = classify(href).canonical or href
        if canonical in seen:
            continue
        seen.add(canonical)

        title = _strip_dom_noise(entry.get("text", ""))
        if len(title) > 160:
            title = title[:160].rstrip()
        thumb = next(
            (src for src in entry.get("imgs", []) if src.startswith(("http://", "https://", "//"))),
            "",
        )
        if thumb.startswith("//"):
            thumb = "https:" + thumb

        info = classify(href)
        candidates.append(
            Candidate(
                url=canonical,
                title=first_line(title, 120),
                platform=info.name,
                author=info.author_hint,
                thumb=thumb,
                engine="Google Lens",
                exact=bool(entry.get("exact", default_exact)),
                extra={"source_host": info.kind},
            )
        )
        if len(candidates) >= limit:
            break
    return candidates


# ---------------------------------------------------------------------------
# 引擎
# ---------------------------------------------------------------------------
_BROWSER_JS = """
() => {
  const clean = (s) => (s || '').replace(/\\s+/g, ' ').trim();
  const items = [];
  const seen = new Set();
  const bad = /(^|\\.)google\\.|gstatic|googleusercontent|schema\\.org/;
  for (const a of document.querySelectorAll('a[href]')) {
    const href = a.href || '';
    if (!/^https?:/.test(href)) continue;
    try {
      if (bad.test(new URL(href).hostname)) continue;
    } catch (e) { continue; }
    if (seen.has(href)) continue;
    let node = a;
    let img = '';
    for (let i = 0; i < 3 && node; i++, node = node.parentElement) {
      const el = node.querySelector('img');
      if (el) { img = el.currentSrc || el.src || ''; break; }
    }
    const text = clean(a.innerText || a.getAttribute('aria-label') || '');
    if (!text && !img) continue;
    seen.add(href);
    items.push({href, text: text.slice(0, 500), img});
  }
  return {
    items,
    url: location.href,
    title: document.title,
    body: clean(document.body ? document.body.innerText : '').slice(0, 3000),
  };
}
"""

_CAPTCHA_MARKERS = (
    "unusual traffic",
    "异常流量",
    "not a robot",
    "人机验证",
    "recaptcha",
    "sorry/index",
)

def _short(exc: BaseException, limit: int = 120) -> str:
    return str(exc).replace("\n", " ")[:limit]


def _on_exact_tab(url: str, exact: bool) -> bool:
    """只有真的落在「完全匹配」（udm=48）页面上，才允许标记为精确。"""
    if not exact:
        return False
    return "udm=48" in (url or "") or "udm%3D48" in (url or "")


class LensEngine:
    engine_name = "Google Lens"

    def __init__(
        self,
        client,
        *,
        backend: str = "auto",
        hl: str = "zh-CN",
        safe_search: bool = False,
        include_visual: bool = True,
        timeout: float = 60.0,
        browser=None,
        browser_error: str = "",
    ):
        self.client = client
        self.backend = (backend or "auto").lower()
        self.hl = hl or "zh-CN"
        self.safe_search = safe_search
        self.include_visual = include_visual
        self.timeout = timeout
        self.browser = browser
        self.browser_error = browser_error

    async def search(self, img: ImageInput, limit: int = 8) -> EngineResult:
        started = time.monotonic()
        result = EngineResult(engine=self.engine_name)
        errors: List[str] = []
        blocked = False

        if self.backend in ("auto", "browser"):
            for attempt in range(2):
                try:
                    result.candidates = await self._browser_flow(img, limit)
                    break
                except LensBlocked as exc:
                    blocked = True
                    errors.append(f"浏览器: {exc}")
                    # 偶发的限流等一两秒就好；真被风控则第二次也一样
                    if attempt == 0:
                        await asyncio.sleep(1.5)
                        continue
                    break
                except Exception as exc:
                    errors.append(f"浏览器: {_short(exc)}")
                    break

        # 浏览器上传都没拿到结果，HTTP 路径基本也只会拿到 JS 空壳；
        # 只有在没有浏览器、或浏览器路径单纯报错时才试。
        if not result.candidates and not blocked and self.backend in ("auto", "http"):
            try:
                page = await self._upload(img)
                exact = await self._search_http(page, limit, exact=True)
                if exact:
                    result.candidates.extend(exact)
                elif self.include_visual:
                    result.candidates.extend(await self._search_http(page, limit, exact=False))
            except LensBlocked as exc:
                errors.append(f"HTTP: {exc}")
            except Exception as exc:
                errors.append(f"HTTP: {_short(exc)}")

        if not result.candidates:
            if self.backend == "off":
                result.error = "已关闭"
            elif errors:
                result.error = "；".join(errors)
            else:
                result.error = "无匹配结果"
        result.elapsed = time.monotonic() - started
        return result

    # ------------------------------------------------------------------
    # 浏览器：上传 + 渲染 + 取链接
    # ------------------------------------------------------------------
    async def _browser_flow(self, img: ImageInput, limit: int) -> List[Candidate]:
        if self.browser is None:
            raise LensError(self.browser_error or "通用浏览器组件未安装")
        page_obj = await self.browser.new_page()
        try:
            result_url = await self._resolve_result_url(page_obj, img)

            await page_obj.goto(_with_udm(result_url, _UDM_EXACT, self.safe_search))
            await page_obj.wait(2400)
            await self._dismiss_consent(page_obj)
            candidates = await self._extract(page_obj, limit, exact=True)
            if candidates or not self.include_visual:
                return candidates

            await page_obj.goto(_with_udm(result_url, _UDM_VISUAL, self.safe_search))
            await page_obj.wait(2000)
            return await self._extract(page_obj, limit, exact=False)
        finally:
            await page_obj.close()

    async def _resolve_result_url(self, page_obj, img: ImageInput) -> str:
        """拿到带 vsrid 的结果地址。

        Lens 首页会 302 到 www.google.com，在 lens 页面里 fetch 相对路径会 404，
        所以这里用「真·multipart 表单提交」：浏览器自己发 POST 并跟随 303，
        既没有 CORS 问题，也不依赖页面停在哪个 origin 上。
        """
        source = (img.source_url or "").strip()
        if source.startswith(("http://", "https://")):
            try:
                resp = await page_obj.request_get(
                    LENS_BY_URL.format(hl=self.hl, url=quote(source, safe="")),
                    max_redirects=0,
                )
                location = resp.header("location")
                if "vsrid=" in location:
                    return location
            except Exception as exc:
                logger.debug(f"picfinder: Lens 按 URL 查询失败（改用上传）: {exc}")

        try:
            return await self._upload_via_form(page_obj, img)
        except LensError:
            raise
        except Exception as exc:
            logger.debug(f"picfinder: Lens 表单上传失败，改用上下文请求: {_short(exc)}")
        return await self._upload_via_request(page_obj, img)

    async def _upload_via_form(self, page_obj, img: ImageInput) -> str:
        data, mime, name = img.upload_parts(max_bytes=2 * 1024 * 1024, max_side=2048)
        action = f"{LENS_UPLOAD}?stcs={int(time.time() * 1000)}&hl={quote(self.hl)}"
        dims = f"{img.width},{img.height}" if img.width and img.height else ""
        html = (
            "<!doctype html><meta charset='utf-8'><body>"
            f"<form id='lens-form' method='POST' enctype='multipart/form-data' action='{action}'>"
            "<input id='lens-file' type='file' name='encoded_image'>"
            + (f"<input type='hidden' name='processed_image_dimensions' value='{dims}'>" if dims else "")
            + "<button id='lens-submit' type='submit'>go</button></form></body>"
        )
        await page_obj.set_content(html)
        await page_obj.set_input_files(
            "#lens-file",
            files=[{"name": name, "mimeType": mime or "image/jpeg", "buffer": data}],
        )
        await page_obj.click(selector="#lens-submit")
        for attempt in range(24):
            await page_obj.wait(250)
            current = await page_obj.url()
            if "vsrid=" in current:
                return current
            if "/sorry/" in current:
                raise LensBlocked("Google 要求人机验证（换住宅/家宽出口，或放自己的 Cookie 再试）")
            if attempt == 8 and "consent." in current:
                await self._dismiss_consent(page_obj)
        raise LensError("上传后没有拿到结果地址（可能被风控）")

    async def _upload_via_request(self, page_obj, img: ImageInput) -> str:
        data, mime, name = img.upload_parts(max_bytes=2 * 1024 * 1024, max_side=2048)
        fields = {}
        if img.width and img.height:
            fields["processed_image_dimensions"] = f"{img.width},{img.height}"
        url = f"{LENS_UPLOAD}?stcs={int(time.time() * 1000)}&hl={quote(self.hl)}"
        try:
            resp = await page_obj.request_post_multipart(
                url,
                fields=fields,
                files={"encoded_image": {"name": name, "mimeType": mime or "image/jpeg", "buffer": data}},
            )
        except Exception as exc:
            raise LensError(f"Lens 上传失败: {_short(exc)}") from exc

        location = resp.header("location")
        if resp.status in (403, 429) or "/sorry/" in location:
            raise LensBlocked(f"Google 拒绝上传（HTTP {resp.status}）")
        if "vsrid=" in location:
            return location
        if resp.status == 404:
            raise LensError("Lens 上传接口 404（Google 改了接口）")
        raise LensError(f"Lens 上传 HTTP {resp.status}")

    async def _extract(self, page_obj, limit: int, exact: bool) -> List[Candidate]:
        payload = await page_obj.evaluate(_BROWSER_JS)
        payload = payload or {}
        body = str(payload.get("body") or "").lower()
        final_url = str(payload.get("url") or "")
        if "/sorry/" in final_url or any(marker in body for marker in _CAPTCHA_MARKERS):
            raise LensBlocked("Google 要求人机验证（换住宅/家宽出口，或放自己的 Cookie 再试）")

        candidates = parse_lens_dom(payload, limit=limit, exact=_on_exact_tab(final_url, exact))
        if not candidates:
            await page_obj.wait(1800)
            payload = (await page_obj.evaluate(_BROWSER_JS)) or {}
            final_url = str(payload.get("url") or final_url)
            candidates = parse_lens_dom(payload, limit=limit, exact=_on_exact_tab(final_url, exact))
        return candidates

    async def _dismiss_consent(self, page_obj) -> None:
        for label in ("全部接受", "接受全部", "Accept all", "I agree", "同意"):
            try:
                await page_obj.click(text=label, timeout_ms=1200)
                await page_obj.wait(800)
                return
            except Exception:
                continue

    # ------------------------------------------------------------------
    # HTTP 兜底
    # ------------------------------------------------------------------
    async def _upload(self, img: ImageInput) -> LensPage:
        payload, mime, filename = img.upload_parts()
        files = {"encoded_image": (filename, payload, mime or "image/jpeg")}
        data = {}
        if img.width and img.height:
            data["processed_image_dimensions"] = f"{img.width},{img.height}"
        params = {"stcs": str(int(time.time() * 1000)), "hl": self.hl}
        try:
            resp = await self.client.post(
                LENS_UPLOAD,
                params=params,
                files=files,
                data=data or None,
                allow_redirects=False,
                timeout=35,
                browser_headers=True,
            )
        except Exception as exc:
            raise LensError(f"Lens 上传请求失败: {exc}") from exc

        if resp.status in (301, 302, 303, 307, 308):
            location = resp.header("location")
            if not location:
                raise LensError("Lens 未返回结果地址")
            return LensPage(url=urljoin(LENS_HOME + "/", unescape(location)))

        if resp.status == 200 and resp.content:
            text = resp.text
            if _looks_like_results(text):
                return LensPage(url=resp.url, html=text)
            if any(marker in text for marker in _SHELL_MARKERS):
                raise LensError("结果页需要 JS 渲染（浏览器路径才拿得到结果）")
            return LensPage(url=resp.url, html=text)

        if resp.status in (429, 403):
            raise LensBlocked(f"Google 拒绝请求 (HTTP {resp.status})")
        raise LensError(f"Lens 上传 HTTP {resp.status}")

    async def _search_http(self, page: LensPage, limit: int, exact: bool) -> List[Candidate]:
        if page.html:
            direct = parse_lens_html(page.html, limit=limit, exact=exact)
            if direct:
                return direct
        if not page.url:
            return []
        target = _with_udm(page.url, _UDM_EXACT if exact else _UDM_VISUAL, self.safe_search)
        resp = await self.client.get(
            target,
            timeout=25,
            browser_headers=True,
            headers={"Referer": LENS_HOME + "/"},
        )
        if resp.status != 200:
            raise LensError(f"结果页 HTTP {resp.status}")
        text = resp.text
        if any(marker in text for marker in _SHELL_MARKERS):
            raise LensError("结果页需要 JS 渲染")
        if any(marker in text for marker in _CAPTCHA_MARKERS) or "/sorry/" in str(resp.url):
            raise LensBlocked("Google 要求人机验证")
        return parse_lens_html(text, limit=limit, exact=exact)
