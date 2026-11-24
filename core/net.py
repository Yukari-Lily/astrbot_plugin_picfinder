"""HTTP 客户端。

优先使用 curl_cffi（Chrome TLS/HTTP2 指纹），在未安装时回退到 httpx。
所有引擎共用同一个实例，便于复用连接与统一代理配置。
"""

from __future__ import annotations

import inspect
import json
import logging
from dataclasses import dataclass
from typing import Any, Mapping, Optional

try:
    from astrbot.api import logger  # type: ignore
except Exception:  # pragma: no cover - 脱离 AstrBot 运行（测试）时
    logger = logging.getLogger("astrbot_plugin_picfinder.net")

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8,ja;q=0.7",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
}

_BROWSER_HEADERS = {
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}


_HTTP2_MARKERS = (
    "HTTP/2",
    "http2",
    "(92)",
    "stream reset",
    "RST_STREAM",
    "INTERNAL_ERROR",
    "PROTOCOL_ERROR",
)


def _looks_like_http2_failure(exc: BaseException) -> bool:
    """curl 的 HTTP/2 层故障（隧道/大 body 最常见），换 HTTP/1.1 通常就好。"""
    text = str(exc)
    return any(marker in text for marker in _HTTP2_MARKERS)


@dataclass
class Response:
    """统一的响应对象，屏蔽 curl_cffi / httpx 的差异。"""

    status: int
    content: bytes
    headers: dict
    url: str

    @property
    def text(self) -> str:
        charset = "utf-8"
        content_type = self.headers.get("content-type", "")
        if "charset=" in content_type:
            charset = content_type.split("charset=", 1)[1].split(";", 1)[0].strip() or "utf-8"
        try:
            return self.content.decode(charset, errors="replace")
        except (LookupError, UnicodeDecodeError):
            return self.content.decode("utf-8", errors="replace")

    def json(self) -> Any:
        return json.loads(self.text)

    def header(self, name: str, default: str = "") -> str:
        return self.headers.get(name.lower(), default) or default


class HttpClient:
    """带代理与浏览器指纹的异步 HTTP 客户端。"""

    def __init__(self, proxy: str = "", timeout: float = 20.0, impersonate: str = "chrome"):
        self.proxy = (proxy or "").strip()
        self.timeout = timeout
        self.impersonate = impersonate
        self._curl = None
        self._httpx = None
        self._mode = ""
        self._closed = False

    # ------------------------------------------------------------------
    # 后端初始化
    # ------------------------------------------------------------------
    def _ensure(self):
        if self._closed:
            raise RuntimeError("HttpClient 已关闭")
        if self._mode:
            return
        try:
            from curl_cffi.requests import AsyncSession  # type: ignore

            proxies = {"all": self.proxy} if self.proxy else None
            self._curl = AsyncSession(
                impersonate=self.impersonate,
                timeout=self.timeout,
                proxies=proxies,
                headers=DEFAULT_HEADERS,
            )
            self._mode = "curl"
            return
        except Exception as exc:  # pragma: no cover - 取决于运行环境
            logger.debug(f"picfinder: curl_cffi 不可用，改用 httpx ({exc})")

        self._ensure_httpx()
        self._mode = "httpx"

    # ------------------------------------------------------------------
    # 请求
    # ------------------------------------------------------------------
    async def request(
        self,
        method: str,
        url: str,
        *,
        params: Optional[Mapping[str, Any]] = None,
        data: Optional[Mapping[str, Any]] = None,
        files: Optional[Mapping[str, Any]] = None,
        headers: Optional[Mapping[str, str]] = None,
        allow_redirects: bool = True,
        timeout: Optional[float] = None,
        browser_headers: bool = False,
    ) -> Response:
        self._ensure()
        merged_headers = dict(DEFAULT_HEADERS)
        if browser_headers:
            merged_headers.update(_BROWSER_HEADERS)
        if headers:
            merged_headers.update({k: v for k, v in headers.items() if v is not None})
        request_timeout = timeout or self.timeout

        if self._mode == "curl":
            try:
                return await self._curl_request(
                    method,
                    url,
                    params=params,
                    data=data,
                    files=files,
                    headers=merged_headers,
                    allow_redirects=allow_redirects,
                    timeout=request_timeout,
                )
            except Exception as exc:
                if not _looks_like_http2_failure(exc):
                    raise
                # 走 SS/Trojan 隧道时 HTTP/2 大 body 偶尔被服务端 RST，
                # 换成 HTTP/1.1（httpx）再发一次，请求内容不变。
                logger.debug(f"picfinder: HTTP/2 失败，改用 HTTP/1.1 重试: {exc}")
                self._ensure_httpx()

        return await self._httpx_request(
            method,
            url,
            params=params,
            data=data,
            files=files,
            headers=merged_headers,
            allow_redirects=allow_redirects,
            timeout=request_timeout,
        )

    async def _curl_request(self, method: str, url: str, **kwargs) -> Response:
        self._ensure()
        files = kwargs.pop("files", None)
        multipart = None
        if files:
            try:
                from curl_cffi import CurlMime  # type: ignore
            except Exception as exc:  # pragma: no cover
                raise RuntimeError(f"curl_cffi 不支持文件上传: {exc}") from exc
            multipart = CurlMime()
            for field, spec in files.items():
                filename, content, content_type = spec
                multipart.addpart(
                    field,
                    filename=str(filename),
                    data=content if isinstance(content, bytes) else str(content).encode(),
                    content_type=content_type,
                )
        try:
            raw = await self._curl.request(
                method,
                url,
                multipart=multipart,
                **{k: v for k, v in kwargs.items() if v is not None},
            )
        finally:
            if multipart is not None:
                try:
                    multipart.close()
                except Exception:
                    pass
        return Response(
            status=int(raw.status_code),
            content=raw.content or b"",
            headers={str(k).lower(): str(v) for k, v in (raw.headers or {}).items()},
            url=str(raw.url),
        )

    def _ensure_httpx(self) -> None:
        if self._httpx is not None:
            return
        import httpx

        mounts = {}
        if self.proxy:
            mounts = {
                "http://": httpx.AsyncHTTPTransport(proxy=self.proxy),
                "https://": httpx.AsyncHTTPTransport(proxy=self.proxy),
            }
        self._httpx = httpx.AsyncClient(
            timeout=self.timeout,
            follow_redirects=True,
            headers=DEFAULT_HEADERS,
            mounts=mounts,
        )

    async def _httpx_request(self, method: str, url: str, **kwargs) -> Response:
        self._ensure_httpx()
        follow = kwargs.pop("allow_redirects", True)
        raw = await self._httpx.request(
            method,
            url,
            follow_redirects=follow,
            **{k: v for k, v in kwargs.items() if v is not None},
        )
        return Response(
            status=int(raw.status_code),
            content=raw.content or b"",
            headers={str(k).lower(): str(v) for k, v in raw.headers.items()},
            url=str(raw.url),
        )

    async def get(self, url: str, **kwargs) -> Response:
        return await self.request("GET", url, **kwargs)

    async def post(self, url: str, **kwargs) -> Response:
        return await self.request("POST", url, **kwargs)

    async def get_bytes(
        self,
        url: str,
        *,
        max_bytes: int = 8 * 1024 * 1024,
        timeout: float = 12.0,
        headers: Optional[Mapping[str, str]] = None,
    ) -> Optional[bytes]:
        """下载二进制内容，限制体积，失败返回 None。"""
        try:
            resp = await self.get(url, timeout=timeout, headers=headers)
        except Exception as exc:
            logger.debug(f"picfinder: 下载失败 {url}: {exc}")
            return None
        if resp.status != 200 or not resp.content:
            logger.debug(f"picfinder: 下载失败 {url}: HTTP {resp.status}")
            return None
        if len(resp.content) > max_bytes:
            logger.debug(f"picfinder: 跳过过大文件 {url} ({len(resp.content)} bytes)")
            return None
        content_type = resp.header("content-type").lower()
        if content_type and not (
            content_type.startswith("image/")
            or content_type.startswith("application/octet-stream")
            or content_type.startswith("binary/")
        ):
            try:
                from .image import looks_like_image
            except (ImportError, ValueError):  # pragma: no cover
                from core.image import looks_like_image  # type: ignore

            if not looks_like_image(resp.content):
                logger.debug(f"picfinder: 非图片响应 {url} ({content_type})")
                return None
        return resp.content

    async def close(self) -> None:
        self._closed = True
        if self._curl is not None:
            try:
                result = self._curl.close()
                if inspect.isawaitable(result):
                    await result
            except Exception:
                pass
            self._curl = None
        if self._httpx is not None:
            try:
                await self._httpx.aclose()
            except Exception:
                pass
            self._httpx = None
        self._mode = ""
