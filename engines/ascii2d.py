"""Ascii2D 二次元搜图引擎（自带完整反 403 客户端）。

为什么要自己实现：PicImageSearch 的 ``Ascii2D`` 只是用裸 httpx ``POST /search/uri``，
既没有 Rails 的 ``authenticity_token``（CSRF）也没有 ``_session_id`` 会话 cookie，
UA 还是 Chrome/99，TLS 指纹是 Python 的。Cloudflare 一看就是脚本，直接回
``403 Attention Required``——这就是插件里那个"Ascii2D 遇 403 熔断"的根因。

本模块自己走完整条浏览器链路：

1. ``GET /`` 拿 ``_session_id`` cookie + 两个表单各自的 ``authenticity_token``；
2. 带 ``Origin`` / ``Referer`` / ``Sec-Fetch-*`` 等真实导航头，用 curl_cffi 的
   Chrome TLS(JA3/JA4)+HTTP2 指纹提交 ``/search/file``（优先，上传本地图片）或
   ``/search/uri``（远程 URL）；
3. 命中拦截时逐级升级：换提交方式 → 轮换指纹画像 → 复用 ``cf_clearance`` →
   可选 Playwright 真浏览器过盾（``Just a moment...`` 只有真 JS 引擎能过）；
4. 自己解析结果页 HTML（不依赖 pyquery），必要时补一次 bovw 特征检索。

所有升级路径都是"能过就过、过不去就平滑降级"，绝不阻塞主搜图流程。
"""

from __future__ import annotations

import asyncio
import os
import random
import re
import secrets
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

try:
    from astrbot.api import logger
except ImportError:  # pragma: no cover - 本地/单测环境
    import logging

    logger = logging.getLogger("astrbot_plugin_picfinder.ascii2d")

from .base import EngineSearchResult, SearchResultItem

try:
    from ..normalizer.platform_matcher import normalize_platform_url
except (ImportError, ValueError):  # pragma: no cover
    from normalizer.platform_matcher import normalize_platform_url

try:
    from ..proxy import ShadowsocksProxy
except (ImportError, ValueError):  # pragma: no cover
    try:
        from proxy import ShadowsocksProxy
    except ImportError:
        ShadowsocksProxy = None  # type: ignore[assignment]

try:
    from curl_cffi.requests import AsyncSession as _CurlAsyncSession

    HAVE_CURL_CFFI = True
except Exception:  # pragma: no cover - 未安装 curl_cffi
    _CurlAsyncSession = None  # type: ignore[assignment]
    HAVE_CURL_CFFI = False

try:
    import httpx

    HAVE_HTTPX = True
except Exception:  # pragma: no cover
    httpx = None  # type: ignore[assignment]
    HAVE_HTTPX = False


BASE_URL = "https://ascii2d.net"
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Ascii2D 结果里的"来源站"标记，用于把 作品链接 / 作者链接 分开
SOURCE_MARKS = ("fanbox", "fantia", "misskey", "pixiv", "twitter", "ニコニコ静画", "ニジエ")

# Cloudflare 两种拦截页的特征
# 注意：正常页面也会注入 cdn-cgi/challenge-platform 脚本，不能拿它当校验特征，
# 否则 200 的真页面会被误判成"人机校验"。
BLOCK_MARKERS = (
    "attention required",
    "you have been blocked",
    "cf-error-details",
    "error code: 1020",
)
CHALLENGE_MARKERS = (
    "just a moment",
    "cf_chl_opt",
    "cf-challenge",
    "_cf_chl_",
    "verifying you are human",
    "challenge-form",
)

# TLS 指纹升级顺序：先默认 chrome，过不了再换几个差异较大的画像
IMPERSONATE_LADDER = ("chrome", "chrome124", "chrome131", "safari17_0", "edge101")

_RAY_RE = re.compile(r"Ray ID:\s*(?:<[^>]*>\s*)*([0-9a-f]{10,})", re.I)
_BOVW_LINK_RE = re.compile(r'href="([^"]*?/bovw[^"]*)"', re.I)
_FORM_RE = re.compile(r"<form\b[^>]*>(.*?)</form>", re.S | re.I)
_FORM_ACTION_RE = re.compile(r'action="([^"]+)"', re.I)
_TOKEN_NAME_FIRST_RE = re.compile(
    r'name="authenticity_token"[^>]*?value="([^"]+)"', re.I
)
_TOKEN_VALUE_FIRST_RE = re.compile(
    r'value="([^"]+)"[^>]*?name="authenticity_token"', re.I
)

_NO_RESULT_MARKERS = ("該当する画像は見つかりません", "no matching", "見つかりませんでした")

_SKIP_TITLE_JUNK = ("詳細掲示板のログ", "2ちゃんねるのログ")


def extract_ray_id(text: str) -> str:
    """从 Cloudflare 拦截页里抠出 Ray ID，方便向站方/自己排查。"""
    m = _RAY_RE.search(text or "")
    return m.group(1) if m else ""


def _header(headers: Optional[Mapping[str, str]], name: str) -> str:
    if not headers:
        return ""
    try:
        value = headers.get(name)
        if value is not None:
            return str(value)
        for key, val in headers.items():
            if str(key).lower() == name.lower():
                return str(val)
    except Exception:  # pragma: no cover - 防御异常 header 容器
        return ""
    return ""


def classify_response(status: int, text: str, headers: Optional[Mapping[str, str]] = None) -> str:
    """把一次响应分成 ok / challenge / blocked / redirect / error。

    challenge = Cloudflare 的 JS 人机校验（真浏览器能过），
    blocked   = 防火墙规则直接拒绝（换 IP / 换 cf_clearance 才有救）。
    """
    low = (text or "").lower()
    if _header(headers, "cf-mitigated").lower() == "challenge":
        return "challenge"
    if any(marker in low for marker in CHALLENGE_MARKERS):
        return "challenge"
    if any(marker in low for marker in BLOCK_MARKERS):
        return "blocked"
    if 200 <= status < 300:
        return "ok"
    if 300 <= status < 400:
        return "redirect"
    if status in (401, 403, 407, 429, 503):
        return "blocked"
    return "error"


def looks_like_challenge(title: str, body: str) -> bool:
    """浏览器场景下判断当前页面是否还停在 Cloudflare 校验页。"""
    title = title or ""
    low = (body or "").lower()
    if "just a moment" in title.lower():
        return True
    return any(marker in low for marker in CHALLENGE_MARKERS)


def parse_tokens(home_html: str) -> Dict[str, str]:
    """解析首页两个表单的 authenticity_token。

    返回 ``{"uri": token, "file": token, "any": 第一个 token}``。
    """
    tokens: Dict[str, str] = {}
    fallback = ""
    for match in _FORM_RE.finditer(home_html or ""):
        body = match.group(1)
        action_match = _FORM_ACTION_RE.search(match.group(0))
        action = (action_match.group(1) if action_match else "").rstrip("/")
        token_match = _TOKEN_NAME_FIRST_RE.search(body) or _TOKEN_VALUE_FIRST_RE.search(body)
        if not token_match:
            continue
        token = token_match.group(1)
        fallback = fallback or token
        if action.endswith("/uri"):
            tokens["uri"] = token
        elif action.endswith("/file"):
            tokens["file"] = token
    if not tokens and not fallback:
        token_match = _TOKEN_NAME_FIRST_RE.search(home_html or "") or _TOKEN_VALUE_FIRST_RE.search(
            home_html or ""
        )
        if token_match:
            fallback = token_match.group(1)
    if fallback:
        tokens.setdefault("any", fallback)
    return tokens


def parse_cookie_string(raw: str) -> Dict[str, str]:
    """把浏览器里复制出来的 ``Cookie:`` 字符串拆成 dict。"""
    cookies: Dict[str, str] = {}
    for chunk in (raw or "").replace("\n", ";").split(";"):
        if "=" not in chunk:
            continue
        key, _, value = chunk.partition("=")
        key = key.strip()
        if key:
            cookies[key] = value.strip()
    return cookies


def encode_multipart(
    fields: Mapping[str, str],
    file_field: str,
    filename: str,
    content: bytes,
    content_type: str = "application/octet-stream",
) -> Tuple[bytes, str]:
    """手搓 multipart/form-data，避开各 HTTP 客户端版本间 files= 的 API 差异。"""
    boundary = "----WebKitFormBoundary" + secrets.token_hex(8)
    chunks: List[bytes] = []
    for name, value in fields.items():
        chunks.append(f"--{boundary}\r\n".encode())
        chunks.append(
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode("utf-8")
        )
    chunks.append(f"--{boundary}\r\n".encode())
    chunks.append(
        (
            f'Content-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'
            f"Content-Type: {content_type}\r\n\r\n"
        ).encode("utf-8")
    )
    chunks.append(content)
    chunks.append(f"\r\n--{boundary}--\r\n".encode())
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def guess_mime(filename: str) -> str:
    low = (filename or "").lower()
    for suffix, mime in (
        (".png", "image/png"),
        (".jpg", "image/jpeg"),
        (".jpeg", "image/jpeg"),
        (".gif", "image/gif"),
        (".webp", "image/webp"),
        (".bmp", "image/bmp"),
    ):
        if low.endswith(suffix):
            return mime
    return "application/octet-stream"


# ----------------------------------------------------------------------
# 极简 DOM：只为了读 Ascii2D 结果页，不引入 bs4 / pyquery / lxml
# ----------------------------------------------------------------------


class _Node:
    __slots__ = ("tag", "attrs", "children", "parent")

    def __init__(self, tag: str, attrs: Dict[str, str], parent: Optional["_Node"] = None):
        self.tag = tag
        self.attrs = attrs
        self.children: List[Any] = []
        self.parent = parent

    @property
    def classes(self) -> Tuple[str, ...]:
        return tuple((self.attrs.get("class") or "").split())


class _TreeBuilder(HTMLParser):
    _VOID = {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node("root", {})
        self._stack: List[_Node] = [self.root]

    def handle_starttag(self, tag: str, attrs) -> None:
        node = _Node(tag, {k: (v if v is not None else "") for k, v in attrs}, self._stack[-1])
        self._stack[-1].children.append(node)
        if tag not in self._VOID:
            self._stack.append(node)

    def handle_startendtag(self, tag: str, attrs) -> None:
        node = _Node(tag, {k: (v if v is not None else "") for k, v in attrs}, self._stack[-1])
        self._stack[-1].children.append(node)

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self._stack) - 1, 0, -1):
            if self._stack[index].tag == tag:
                del self._stack[index:]
                return

    def handle_data(self, data: str) -> None:
        self._stack[-1].children.append(data)


def parse_html(text: str) -> _Node:
    builder = _TreeBuilder()
    builder.feed(text or "")
    builder.close()
    return builder.root


def _descendants(node: _Node):
    for child in node.children:
        if isinstance(child, _Node):
            yield child
            yield from _descendants(child)


def find_all(node: _Node, tag: Optional[str] = None, classes: Sequence[str] = ()) -> List[_Node]:
    need = set(classes)
    out: List[_Node] = []
    for child in _descendants(node):
        if tag and child.tag != tag:
            continue
        if need and not need.issubset(set(child.classes)):
            continue
        out.append(child)
    return out


def find(node: _Node, tag: Optional[str] = None, classes: Sequence[str] = ()) -> Optional[_Node]:
    for child in find_all(node, tag, classes):
        return child
    return None


def node_text(node: Optional[_Node], skip_tags: Sequence[str] = ()) -> str:
    """取节点可见文本（跳过 script/style 与指定标签），压缩空白。"""
    if node is None:
        return ""
    skip = set(skip_tags) | {"script", "style"}
    parts: List[str] = []

    def walk(current: _Node) -> None:
        for child in current.children:
            if isinstance(child, _Node):
                if child.tag in skip:
                    continue
                if child.tag in ("br", "p", "div", "li", "tr", "h6", "small"):
                    parts.append(" ")
                walk(child)
                if child.tag in ("br", "p", "div", "li", "tr", "h6", "small"):
                    parts.append(" ")
            else:
                parts.append(str(child))

    walk(node)
    return re.sub(r"\s+", " ", "".join(parts)).strip()


def _absolute(url: str, base_url: str) -> str:
    url = (url or "").strip()
    if not url:
        return ""
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("/"):
        return base_url.rstrip("/") + url
    return url


def parse_results(html_text: str, base_url: str = BASE_URL) -> List[Dict[str, Any]]:
    """解析 Ascii2D 结果页，返回原始条目 dict 列表。

    结构（与 ascii2d 现网模板一致）::

        div.row.item-box           单个结果（第一条是"你搜的图"，没有 detail-box）
          div.hash                 感知哈希
          small                    尺寸 / 格式 / 大小
          img                      缩略图
          div.detail-box.gray-link 来源信息：a[0]=作品链接  a[1]=作者链接
                                   small=来源站标记（pixiv / twitter ...）
    """
    root = parse_html(html_text)
    rows = find_all(root, "div", ("row", "item-box")) or find_all(root, "div", ("item-box",))
    results: List[Dict[str, Any]] = []
    for row in rows:
        info = find(row, "div", ("detail-box", "gray-link")) or find(row, "div", ("detail-box",))
        if info is None:
            # "你搜索的图片"那一行没有 detail-box，跳过
            continue

        image = find(row, "img")
        thumbnail = _absolute(image.attrs.get("src", "") if image else "", base_url)

        hash_node = find(row, "div", ("hash",))
        hash_value = node_text(hash_node)

        own_smalls = [n for n in find_all(row, "small") if not _is_inside(n, info)]
        detail = node_text(own_smalls[0]) if own_smalls else ""

        mark = ""
        for small in find_all(info, "small"):
            text = node_text(small)
            if text in SOURCE_MARKS:
                mark = text
                break

        links: List[Dict[str, str]] = []
        for anchor in find_all(info, "a"):
            href = _absolute(anchor.attrs.get("href", ""), base_url)
            if href:
                links.append({"href": href, "text": node_text(anchor)})

        url = ""
        title = ""
        author = ""
        author_url = ""
        if links:
            url = links[0]["href"]
            title = links[0]["text"]
            if len(links) > 1:
                author = links[1]["text"]
                author_url = links[1]["href"]
                if mark and not title:
                    title = links[0]["text"]

        if not url:
            right = find(row, "div", ("pull-xs-right",))
            anchor = find(right, "a") if right else None
            if anchor and anchor.attrs.get("href"):
                url = _absolute(anchor.attrs["href"], base_url)
                title = title or node_text(anchor)

        if not title:
            title = node_text(info, skip_tags=("a",)) or node_text(find(row, "h6"))

        for junk in _SKIP_TITLE_JUNK:
            if junk in title:
                title = ""

        title = re.sub(r"\s+", " ", title).strip()
        if not url and not title:
            continue

        results.append(
            {
                "hash": hash_value,
                "detail": detail,
                "thumbnail": thumbnail,
                "url": url,
                "title": title,
                "author": author,
                "author_url": author_url,
                "source": mark,
                "links": links,
            }
        )
    return results


def _is_inside(node: _Node, ancestor: _Node) -> bool:
    current = node.parent
    while current is not None:
        if current is ancestor:
            return True
        current = current.parent
    return False


def find_bovw_url(html_text: str, page_url: str, base_url: str = BASE_URL) -> str:
    """找结果页上的"特征检索(bovw)"入口，找不到就用 URL 规则兜底。"""
    match = _BOVW_LINK_RE.search(html_text or "")
    if match:
        return _absolute(match.group(1), base_url)
    if "/color/" in page_url:
        return page_url.replace("/color/", "/bovw/")
    if page_url.endswith("/color"):
        return page_url[: -len("/color")] + "/bovw"
    return ""


# ----------------------------------------------------------------------
# 传输层：curl_cffi（浏览器指纹）优先，httpx 兜底
# ----------------------------------------------------------------------


class _Transport:
    """一次搜索会话用的 HTTP 客户端，负责 cookie 保持与浏览器化请求头。"""

    def __init__(
        self,
        base_url: str,
        kind: str,
        impersonate: str,
        proxy: str,
        timeout: float,
        cookies: Optional[Mapping[str, str]] = None,
        proxy_httpx: str = "",
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.kind = kind
        self.impersonate = impersonate or "chrome"
        self.proxy = (proxy or "").strip()
        self.proxy_httpx = (proxy_httpx or "").strip() or self.proxy
        self.timeout = timeout
        self.cookies = dict(cookies or {})
        self._curl = None
        self._httpx = None

    def _headers(self, extra: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
        headers = {
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "image/avif,image/webp,image/apng,*/*;q=0.8"
            ),
            "Accept-Language": "ja,en-US;q=0.9,zh-CN;q=0.8,zh;q=0.7",
            "Cache-Control": "max-age=0",
            "Origin": self.base_url,
            "Referer": self.base_url + "/",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-User": "?1",
            "Upgrade-Insecure-Requests": "1",
        }
        if self.kind != "curl":
            headers["User-Agent"] = DEFAULT_UA
        if extra:
            headers.update({k: v for k, v in extra.items() if v})
        return headers

    async def __aenter__(self) -> "_Transport":
        if self.kind == "curl" and HAVE_CURL_CFFI:
            self._curl = _CurlAsyncSession(
                impersonate=self.impersonate,
                timeout=self.timeout,
                proxy=self.proxy or None,
                cookies=self.cookies or None,
                headers=self._headers(),
            )
        else:
            if not HAVE_HTTPX:
                raise RuntimeError("curl_cffi 与 httpx 均不可用，无法请求 Ascii2D")
            self.kind = "httpx"
            transport_kwargs = {}
            if self.proxy_httpx:
                transport_kwargs["proxy"] = self.proxy_httpx
            client_kwargs = dict(
                timeout=self.timeout,
                headers=self._headers(),
                cookies=self.cookies or None,
                follow_redirects=True,
                http2=True,
                **transport_kwargs,
            )
            try:
                self._httpx = httpx.AsyncClient(**client_kwargs)
            except Exception as exc:
                # 某些环境把 NO_PROXY 写成 "::1,[::1]" 之类，httpx 解析会直接抛 InvalidURL，
                # 这时退化成"不读环境代理"，别让一条坏环境变量毁掉整个引擎。
                logger.debug(f"httpx 读取系统代理失败（{exc}），改用 trust_env=False")
                self._httpx = httpx.AsyncClient(trust_env=False, **client_kwargs)
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    async def close(self) -> None:
        try:
            if self._curl is not None:
                await self._curl.close()
        except Exception:  # pragma: no cover
            pass
        try:
            if self._httpx is not None:
                await self._httpx.aclose()
        except Exception:  # pragma: no cover
            pass
        self._curl = None
        self._httpx = None

    async def get(self, path: str) -> Tuple[int, str, Dict[str, str]]:
        url = path if path.startswith("http") else self.base_url + path
        if self._curl is not None:
            resp = await self._curl.get(url, headers=self._headers())
            return resp.status_code, resp.text, dict(resp.headers)
        resp = await self._httpx.get(url)
        return resp.status_code, resp.text, dict(resp.headers)

    async def post_form(self, path: str, data: Mapping[str, str]) -> Tuple[int, str, Dict[str, str]]:
        url = path if path.startswith("http") else self.base_url + path
        headers = self._headers(
            {"Content-Type": "application/x-www-form-urlencoded", "Sec-Fetch-Site": "same-origin"}
        )
        if self._curl is not None:
            resp = await self._curl.post(url, data=dict(data), headers=headers)
            return resp.status_code, resp.text, dict(resp.headers)
        resp = await self._httpx.post(url, data=dict(data), headers=headers)
        return resp.status_code, resp.text, dict(resp.headers)

    async def post_multipart(
        self, path: str, body: bytes, content_type: str
    ) -> Tuple[int, str, Dict[str, str]]:
        url = path if path.startswith("http") else self.base_url + path
        headers = self._headers({"Content-Type": content_type})
        if self._curl is not None:
            resp = await self._curl.post(url, data=body, headers=headers)
            return resp.status_code, resp.text, dict(resp.headers)
        resp = await self._httpx.post(url, content=body, headers=headers)
        return resp.status_code, resp.text, dict(resp.headers)

    def cookie_dict(self) -> Dict[str, str]:
        try:
            if self._curl is not None:
                return {str(k): str(v) for k, v in dict(self._curl.cookies).items()}
            if self._httpx is not None:
                return {str(k): str(v) for k, v in self._httpx.cookies.items()}
        except Exception:  # pragma: no cover
            return {}
        return {}


class Ascii2DBlocked(RuntimeError):
    """站点在"取 token"阶段就被拦下（首页就 403/校验）。"""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


@dataclass
class _Submission:
    status: int
    text: str
    headers: Dict[str, str]
    url: str
    kind: str
    ray_id: str = ""


# ----------------------------------------------------------------------
# 引擎
# ----------------------------------------------------------------------


class Ascii2DEngine:
    """Ascii2D 搜图引擎（反 403 客户端）。

    参数
    ----
    proxy:  http:// 或 socks5:// 代理；建议日本/住宅线路（Ascii2D 对机房段很敏感）
    cookie: 浏览器里 ``cf_clearance`` 等 cookie 串，``a=b; c=d`` 格式，可留空
    base_url: 站点根地址，便于指向镜像/自建反代
    impersonate: 首选 TLS 指纹画像，默认 chrome
    bovw: 色调检索结果不足时，是否补一次 bovw 特征检索
    browser_fallback: 是否允许用 Playwright 真浏览器过 Cloudflare 人机校验
    browser_executable: 自备 Chromium/Chrome 路径（留空则用 Playwright 自带；
        也可用环境变量 ASCII2D_CHROME_PATH 指定，Docker/ARM 环境常用）
    ss_uri: 内置 Shadowsocks 节点来源。支持 ``ss://`` 链接、Clash JSON/YAML
        （``proxies:``）、sing-box JSON（``outbounds:``）、base64 订阅。填了就在本机起
        一个混合代理口，Ascii2D 的流量全部从这条隧道出去，优先级高于 ``proxy``
    ss_node: 多节点配置时按名字子串挑一个（留空取第一个 ss 节点）
    """

    def __init__(
        self,
        proxy: str = "",
        cookie: str = "",
        base_url: str = BASE_URL,
        impersonate: str = "chrome",
        timeout: float = 25.0,
        bovw: bool = True,
        browser_fallback: bool = True,
        browser_headless: bool = True,
        browser_executable: str = "",
        ss_uri: str = "",
        ss_node: str = "",
        max_attempts: int = 4,
    ) -> None:
        self.proxy = (proxy or "").strip()
        self.base_url = (base_url or BASE_URL).rstrip("/")
        self.impersonate = (impersonate or "chrome").strip()
        self.timeout = float(timeout)
        self.bovw = bool(bovw)
        self.browser_fallback = bool(browser_fallback)
        self.browser_headless = bool(browser_headless)
        self.browser_executable = (
            (browser_executable or os.environ.get("ASCII2D_CHROME_PATH", "")).strip()
        )
        self.max_attempts = max(1, int(max_attempts))
        self.ss_uri = (ss_uri or "").strip()
        self.ss_node = (ss_node or "").strip()
        self._ss_proxy = (
            ShadowsocksProxy(self.ss_uri, select=self.ss_node)
            if (self.ss_uri and ShadowsocksProxy is not None)
            else None
        )
        self._ss_warned = False
        self._cookies: Dict[str, str] = parse_cookie_string(cookie)
        self._file_cache: Dict[str, bytes] = {}
        self._browser_lock: Optional[asyncio.Lock] = None

    # -- 对外接口 -----------------------------------------------------

    async def search(
        self,
        image_url: str,
        local_path: Optional[str] = None,
        max_results: int = 2,
    ) -> EngineSearchResult:
        """执行 Ascii2D 检索（色调检索优先，必要时补 bovw 特征检索）。"""
        items: List[SearchResultItem] = []
        failures: List[str] = []
        kinds_seen: set = set()

        plan = self._plan(has_file=bool(local_path))
        browser_tried = False

        for index, (mode, kind, profile) in enumerate(plan[: self.max_attempts]):
            if index:
                await asyncio.sleep(random.uniform(0.12, 0.45))
            label = f"{mode}/{kind}"
            try:
                submission = await self._submit(mode, kind, profile, image_url, local_path)
            except Ascii2DBlocked as exc:  # 连首页都进不去：换策略，必要时上浏览器
                kinds_seen.add(exc.kind)
                failures.append(f"{label}: {exc}")
                logger.debug(f"Ascii2D [{label}] 被拦截: {exc}")
                if exc.kind == "challenge" and not browser_tried:
                    browser_tried = True
                    browser_items = await self._try_browser(
                        image_url, local_path, max_results
                    )
                    if browser_items:
                        return self._result(browser_items)
                continue
            except Exception as exc:  # 网络层异常：换下一条策略
                failures.append(f"{label}: {exc}")
                logger.debug(f"Ascii2D [{label}] 请求异常: {exc}")
                continue

            kinds_seen.add(submission.kind)
            if submission.kind == "ok":
                parsed, page_url = self._parse_page(submission, mode)
                if len(parsed) < max_results and self.bovw:
                    parsed = await self._supplement_bovw(parsed, submission.text, page_url, mode)
                items = self._to_items(parsed, mode, max_results)
                if items:
                    logger.info(
                        f"Ascii2D [{label}] 命中 {len(items)} 条结果"
                        + (f"（Ray ID {submission.ray_id}）" if submission.ray_id else "")
                    )
                    return self._result(items)
                return EngineSearchResult(
                    engine_name="Ascii2D",
                    items=[],
                    error="Ascii2D 未检索到相似来源（已被站点风控放行，属正常空结果）",
                    best_similarity=0.0,
                )

            if submission.kind == "challenge" and not browser_tried:
                browser_tried = True
                browser_items = await self._try_browser(image_url, local_path, max_results)
                if browser_items:
                    return self._result(browser_items)

            failures.append(f"{label}: {self._summarize(submission)}")
            logger.debug(f"Ascii2D [{label}] 未通过: {self._summarize(submission)}")

        return EngineSearchResult(
            engine_name="Ascii2D",
            items=[],
            error=self._final_error(kinds_seen, failures),
            best_similarity=0.0,
        )

    # -- 内部实现 -----------------------------------------------------

    async def _try_browser(
        self, image_url: str, local_path: Optional[str], max_results: int
    ) -> List[SearchResultItem]:
        """触发 Playwright 真浏览器过盾，成功则直接返回结果条目。"""
        if not self.browser_fallback:
            return []
        logger.info("Ascii2D 触发 Cloudflare 人机校验，尝试 Playwright 真浏览器过盾...")
        try:
            page = await self._browser_search(image_url, local_path)
        except Exception as exc:
            logger.debug(f"Ascii2D [browser] 失败: {exc}")
            return []
        if not page:
            return []

        html_text, page_url = page
        mode = "file" if local_path else "uri"
        parsed, page_url = self._parse_page_text(html_text, page_url)
        if len(parsed) < max_results and self.bovw:
            parsed = await self._supplement_bovw(parsed, html_text, page_url, mode)
        items = self._to_items(parsed, mode, max_results)
        if items:
            logger.info(f"Ascii2D [browser] 过盾成功，命中 {len(items)} 条结果")
        return items

    async def _proxy_pair(self) -> Tuple[str, str]:
        """解析本次请求要用的代理：内置 SS 隧道 > 显式 proxy > 环境变量。

        返回 ``(curl 用, httpx 用)``。内置 SS 代理是混合口：SOCKS5 给 curl_cffi
        （保留浏览器 TLS 指纹），HTTP CONNECT 给 httpx（免装 socksio）。
        """
        if self._ss_proxy is not None:
            if await self._ss_proxy.ensure_started():
                return self._ss_proxy.socks_url, self._ss_proxy.http_url
            if not self._ss_warned:
                self._ss_warned = True
                logger.warning(
                    f"Ascii2D: 内置 Shadowsocks 节点不可用（{self.ss_uri.split('@')[-1][:40]}…），"
                    "本次退回直连/系统代理"
                )
        return self._split_proxy(self.proxy)

    @staticmethod
    def _split_proxy(proxy: str) -> Tuple[str, str]:
        proxy = (proxy or "").strip()
        if not proxy:
            return "", ""
        if proxy.lower().startswith(("socks5", "socks4", "socks")):
            # httpx 走 socks 需要 socksio；这里把 socks 只交给 curl_cffi，
            # httpx 分支则回落到直连（失败会自然降级到其他策略）。
            return proxy, ""
        return proxy, proxy

    async def aclose(self) -> None:
        """释放内置 SS 代理监听口（插件停用时调用）。"""
        if self._ss_proxy is not None:
            await self._ss_proxy.stop()

    def _plan(self, has_file: bool) -> List[Tuple[str, str, str]]:
        """生成策略阶梯：换提交方式 → 换指纹画像 → 退到 httpx。"""
        profiles = [self.impersonate] + [
            p for p in IMPERSONATE_LADDER if p != self.impersonate
        ]
        plan: List[Tuple[str, str, str]] = []
        for profile in profiles[:2]:
            if has_file:
                plan.append(("file", "curl", profile))
                plan.append(("uri", "curl", profile))
            else:
                plan.append(("uri", "curl", profile))
        if has_file:
            plan.append(("file", "httpx", ""))
        plan.append(("uri", "httpx", ""))
        return plan

    async def _submit(
        self,
        mode: str,
        kind: str,
        profile: str,
        image_url: str,
        local_path: Optional[str],
    ) -> _Submission:
        """一次完整提交：先取 token/cookie，再 POST 搜索。"""
        socks_proxy, http_proxy = await self._proxy_pair()
        async with _Transport(
            self.base_url,
            kind,
            profile,
            socks_proxy,
            self.timeout,
            self._cookies,
            proxy_httpx=http_proxy,
        ) as transport:
            home_status, home_text, home_headers = await transport.get("/")
            home_kind = classify_response(home_status, home_text, home_headers)
            if home_kind in ("blocked", "challenge"):
                ray_id = extract_ray_id(home_text)
                raise Ascii2DBlocked(
                    home_kind,
                    f"首页即被拦截（{home_kind}, HTTP {home_status}）"
                    + (f" Ray ID {ray_id}" if ray_id else ""),
                )

            tokens = parse_tokens(home_text)
            token = tokens.get("file" if mode == "file" else "uri") or tokens.get("any", "")
            if not token:
                raise RuntimeError("首页未解析到 authenticity_token（模板可能已变）")

            if mode == "file" and local_path:
                content = self._read_file(local_path)
                body, content_type = encode_multipart(
                    {"utf8": "✓", "authenticity_token": token},
                    "file",
                    _basename(local_path),
                    content,
                    guess_mime(local_path),
                )
                status, text, headers = await transport.post_multipart(
                    "/search/file", body, content_type
                )
                request_url = self.base_url + "/search/file"
            else:
                status, text, headers = await transport.post_form(
                    "/search/uri",
                    {"utf8": "✓", "authenticity_token": token, "uri": image_url},
                )
                request_url = self.base_url + "/search/uri"

            # 保留服务端下发的 cf_clearance / __cf_bm 等，后续策略直接复用
            for key, value in transport.cookie_dict().items():
                self._cookies.setdefault(key, value)

        return _Submission(
            status=status,
            text=text,
            headers=headers,
            url=request_url,
            kind=classify_response(status, text, headers),
            ray_id=extract_ray_id(text),
        )

    def _parse_page(self, submission: _Submission, mode: str) -> Tuple[List[Dict[str, Any]], str]:
        return self._parse_page_text(submission.text, submission.url)

    def _parse_page_text(self, html_text: str, page_url: str) -> Tuple[List[Dict[str, Any]], str]:
        if any(marker in html_text for marker in _NO_RESULT_MARKERS):
            return [], page_url
        return parse_results(html_text, self.base_url), page_url

    async def _supplement_bovw(
        self,
        parsed: List[Dict[str, Any]],
        html_text: str,
        page_url: str,
        mode: str,
    ) -> List[Dict[str, Any]]:
        """色调检索结果不够时，补一次 bovw 特征检索（裁剪/改色图更容易命中）。"""
        bovw_url = find_bovw_url(html_text, page_url, self.base_url)
        if not bovw_url:
            return parsed
        try:
            socks_proxy, http_proxy = await self._proxy_pair()
            async with _Transport(
                self.base_url,
                "curl" if HAVE_CURL_CFFI else "httpx",
                self.impersonate,
                socks_proxy,
                self.timeout,
                self._cookies,
                proxy_httpx=http_proxy,
            ) as transport:
                status, text, headers = await transport.get(bovw_url)
        except Exception as exc:  # pragma: no cover - 网络异常
            logger.debug(f"Ascii2D bovw 补检异常: {exc}")
            return parsed
        if classify_response(status, text, headers) != "ok":
            return parsed

        extra = parse_results(text, self.base_url)
        seen = {item.get("url") for item in parsed if item.get("url")}
        merged = list(parsed)
        for item in extra:
            if item.get("url") and item["url"] in seen:
                continue
            if item.get("url"):
                seen.add(item["url"])
            item["mode"] = "bovw"
            merged.append(item)
        return merged

    async def _browser_search(
        self, image_url: str, local_path: Optional[str]
    ) -> Optional[Tuple[str, str]]:
        """用 Playwright 真浏览器过 Cloudflare 校验并完成检索。

        只有真 JS 引擎能过 ``Just a moment...``；过完后浏览器内的结果页 HTML
        直接拿来解析，同时把 cf_clearance 回填给后续 curl_cffi 请求。
        """
        try:
            from playwright.async_api import async_playwright
        except Exception:
            logger.info(
                "Ascii2D: 未安装 playwright，跳过浏览器过盾"
                "（pip install playwright && playwright install chromium 可启用）"
            )
            return None

        if self._browser_lock is None:
            self._browser_lock = asyncio.Lock()

        timeout_ms = int(self.timeout * 1000)
        async with self._browser_lock:
            async with async_playwright() as pw:
                launch_kwargs: Dict[str, Any] = {
                    "headless": self.browser_headless,
                    "executable_path": self.browser_executable or None,
                    "args": [
                        "--no-sandbox",
                        "--disable-blink-features=AutomationControlled",
                        "--disable-dev-shm-usage",
                    ],
                }
                browser = await pw.chromium.launch(**launch_kwargs)
                try:
                    context_kwargs: Dict[str, Any] = {
                        "locale": "ja-JP",
                        "user_agent": DEFAULT_UA,
                        "viewport": {"width": 1366, "height": 900},
                    }
                    browser_proxy = await self._browser_proxy_url()
                    if browser_proxy:
                        context_kwargs["proxy"] = {"server": browser_proxy}
                    context = await browser.new_context(**context_kwargs)
                    await context.add_init_script(
                        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
                    )
                    page = await context.new_page()
                    await page.goto(self.base_url + "/", wait_until="domcontentloaded", timeout=timeout_ms)
                    if not await self._pass_challenge(page, timeout_ms):
                        logger.info("Ascii2D: 浏览器仍卡在 Cloudflare 校验页，放弃该路径")
                        return None

                    try:
                        if local_path:
                            await page.set_input_files("#file-form input[type=file]", local_path)
                            await page.evaluate(
                                "document.getElementById('file_upload').submit()"
                            )
                        else:
                            await page.fill("#uri-form", image_url)
                            await page.evaluate(
                                "document.getElementById('uri-form').form.submit()"
                            )
                        await page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
                    except Exception as exc:
                        logger.debug(f"Ascii2D: 浏览器提交表单失败: {exc}")
                        return None

                    if not await self._pass_challenge(page, timeout_ms):
                        return None

                    html_text = await page.content()
                    page_url = page.url
                    try:
                        for cookie in await context.cookies():
                            if cookie.get("name"):
                                self._cookies.setdefault(cookie["name"], cookie.get("value", ""))
                    except Exception:  # pragma: no cover
                        pass
                    return html_text, page_url
                finally:
                    try:
                        await browser.close()
                    except Exception:  # pragma: no cover
                        pass

    async def _browser_proxy_url(self) -> str:
        """浏览器只能用 HTTP 代理，内置 SS 的混合口正好两种都讲。"""
        if self._ss_proxy is not None and await self._ss_proxy.ensure_started():
            return self._ss_proxy.http_url
        socks_proxy, http_proxy = self._split_proxy(self.proxy)
        return http_proxy or socks_proxy

    async def _pass_challenge(self, page, timeout_ms: int) -> bool:
        """轮询等待 Cloudflare 校验通过（最多 timeout 秒）。"""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_ms / 1000.0
        while loop.time() < deadline:
            try:
                title = await page.title()
                body = await page.content()
            except Exception:  # pragma: no cover - 页面正在跳转
                await page.wait_for_timeout(1000)
                continue
            if not looks_like_challenge(title, body):
                return True
            if "you have been blocked" in (body or "").lower():
                return False
            await page.wait_for_timeout(1200)
        return False

    def _read_file(self, path: str) -> bytes:
        cached = self._file_cache.get(path)
        if cached is not None:
            return cached
        with open(path, "rb") as handle:
            content = handle.read()
        self._file_cache[path] = content
        return content

    def _to_items(
        self, parsed: List[Dict[str, Any]], mode: str, max_results: int
    ) -> List[SearchResultItem]:
        items: List[SearchResultItem] = []
        seen: set = set()
        for entry in parsed:
            if len(items) >= max_results:
                break
            url = entry.get("url") or ""
            if url and url in seen:
                continue
            if url:
                seen.add(url)

            title = entry.get("title") or ""
            author = entry.get("author") or ""
            normalized = normalize_platform_url(url, title=title) if url else None
            if normalized is not None and not author:
                author = normalized.author

            similarity = 80.0 if entry.get("mode") == "bovw" else 75.0
            items.append(
                SearchResultItem(
                    title=title,
                    url=(normalized.canonical_url if normalized and normalized.canonical_url else url),
                    platform=normalized.platform_name if normalized else "网页",
                    author=author,
                    thumbnail=entry.get("thumbnail") or "",
                    similarity=similarity,
                    source_engine="Ascii2D",
                    is_exact_match=False,
                    extra_info={
                        "badge": normalized.badge if normalized else "🌐 [网页]",
                        "hash": entry.get("hash", ""),
                        "detail": entry.get("detail", ""),
                        "source": entry.get("source", ""),
                        "author_url": entry.get("author_url", ""),
                        "mode": entry.get("mode", "color"),
                    },
                )
            )
        return items

    @staticmethod
    def _result(items: List[SearchResultItem]) -> EngineSearchResult:
        best = max((item.similarity for item in items), default=0.0)
        return EngineSearchResult(
            engine_name="Ascii2D",
            items=items,
            error=None,
            best_similarity=best,
        )

    @staticmethod
    def _summarize(submission: _Submission) -> str:
        if submission.kind == "blocked":
            return f"HTTP {submission.status} 防火墙拦截" + (
                f"（Ray ID {submission.ray_id}）" if submission.ray_id else ""
            )
        if submission.kind == "challenge":
            return f"HTTP {submission.status} 人机校验未通过"
        return f"HTTP {submission.status} 响应异常"

    @staticmethod
    def _final_error(kinds_seen: set, failures: List[str]) -> str:
        if "blocked" in kinds_seen:
            return (
                "Ascii2D 被 Cloudflare 直接拦截（HTTP 403，机房/风控 IP 段最常见）："
                "为该引擎单独配置日本住宅代理（ascii2d_proxy），"
                "或在 ascii2d_cookie 里粘贴浏览器中的 cf_clearance 后重试"
            )
        if "challenge" in kinds_seen:
            return (
                "Ascii2D 触发 Cloudflare 人机校验（Just a moment...）：已尝试指纹轮换与浏览器过盾，"
                "仍被拦。可安装 playwright 启用真浏览器过盾"
                "（pip install playwright && playwright install chromium），或配置 ascii2d_proxy"
            )
        detail = "；".join(failures[:3]) if failures else "无可用策略"
        return f"Ascii2D 检索失败: {detail}"


def _basename(path: str) -> str:
    try:
        return os.path.basename(path) or "image.png"
    except Exception:  # pragma: no cover
        return "image.png"


__all__ = [
    "Ascii2DEngine",
    "BASE_URL",
    "classify_response",
    "extract_ray_id",
    "find_bovw_url",
    "looks_like_challenge",
    "parse_cookie_string",
    "parse_html",
    "parse_results",
    "parse_tokens",
]
