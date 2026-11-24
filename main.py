"""Google Lens 优先的精确识图插件（astrbot_plugin_picfinder v1.0）。

触发方式只有三种（触发词：识图/搜图/查图/找图/识图喵，出现在消息里任何位置都认）：
- 同一条消息里既有触发词、又有图片；
- 回复一张图片，再发触发词；
- 整条消息只有触发词 → 进入连续搜图模式（发，快发喵 / 怎么才N张喵～），之后发图。
唤醒词、单纯 @ 机器人、带图的普通聊天都不触发。

结果格式遵循原版 HoshinoBot `picfinder_take`：合并转发 + 缩略图 +
[服务][链接] 相似度:xx% + 出处信息。同一插件内置通用浏览器工具（browser_*），
和搜图共用浏览器进程。
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

try:
    from .browser_core import (
        BrowserConfig,
        BrowserError,
        BrowserManager,
        BrowserPage,
        evaluate_to_text,
        format_snapshot,
        is_blank_page,
        normalize_url,
    )
except ImportError:  # 源码/单测环境
    from browser_core import (  # type: ignore
        BrowserConfig,
        BrowserError,
        BrowserManager,
        BrowserPage,
        evaluate_to_text,
        format_snapshot,
        is_blank_page,
        normalize_url,
    )
try:
    from .proxy import ProxyService
except ImportError:  # 单测/源码环境
    from proxy import ProxyService  # type: ignore
from .core.egress import probe
from .core.image import ImageInput, looks_blank
from .core.net import HttpClient
from .core.legacy_format import LegacyBlock, build_legacy_blocks
from .core.pipeline import Pipeline, PipelineConfig
from .core.trigger import is_bare_keyword, keyword_hit, should_search
from .message_utils import resolve_event_image

_REGISTER_VERSION = "1.0"
_REPO = "https://github.com/Yukari-Lily/astrbot_plugin_picfinder"
_DEFAULT_TRIGGERS = ("识图", "搜图", "查图", "找图", "识图喵")
_LENS_BACKEND = "auto"
_LENS_SAFE_SEARCH = False
_LENS_INCLUDE_VISUAL = True
_ENGINE_TIMEOUT = 45.0
_SAUCENAO_THRESHOLD = 65.0
_RESULT_NUM = 2
_MAX_RESULTS = 3
_BOT_NICKNAME = "picfinder"
_BROWSER_TIMEOUT = 30
_BROWSER_HEADLESS = True
_BROWSER_ALLOW_LOCAL = False
_BROWSER_AUTO_INSTALL = True
_BROWSER_TIMEZONE = "Asia/Shanghai"
_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
_COOKIE_FILES = ("cookies.txt", "cookies.json")


@dataclass

class _BatchSession:
    user_id: str
    expires: float
    count: int
    task: Optional[asyncio.Task] = None


@register("astrbot_plugin_picfinder", "Yukari Lily", "Google Lens 优先的精确识图", _REGISTER_VERSION, _REPO)

class PicFinderPlugin(Star):
    """以图搜图：先找精确出处，找不到就直说找不到。"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.cookie_file = _find_cookie_file()
        if self.cookie_file:
            logger.info(f"picfinder: 已加载浏览器 Cookie {self.cookie_file}")
        self.max_results = _MAX_RESULTS
        self.chain_reply = True
        self.thumb_on = True
        self.result_num = _RESULT_NUM
        self.batch_timeout = max(10, _as_int(config.get("batch_timeout", 60), 60))
        self.bot_nickname = _BOT_NICKNAME
        self._setup_proxy()
        self.browser = BrowserManager(self._build_browser_config(self.proxy_browser_url))
        self.tool_proxy_needs_service = False
        tool_proxy = str(self.config.get("browser_tools_proxy", "inherit") or "inherit").strip()
        if tool_proxy.lower() in ("", "direct", "none", "off"):
            tool_proxy_url = ""
        elif tool_proxy.lower() == "inherit":
            tool_proxy_url = self.proxy_browser_url
            self.tool_proxy_needs_service = self.proxy_service is not None
        elif _is_proxy_url(tool_proxy):
            tool_proxy_url = tool_proxy.replace("socks5h://", "socks5://")
        else:
            tool_proxy_url = ""
            logger.warning(f"picfinder: browser_tools_proxy 不是 URL/inherit/direct，浏览器工具改走直连: {tool_proxy}")
        self.tool_browser = (
            self.browser
            if tool_proxy_url == self.proxy_browser_url
            else BrowserManager(self._build_browser_config(tool_proxy_url, persist=False))
        )
        self._browser_lock = asyncio.Lock()
        self._tabs: list[BrowserPage] = []
        self._active: Optional[BrowserPage] = None
        self.pipeline = Pipeline(self._build_pipeline_config(), browser=self.browser)
        self.image_client = HttpClient(proxy="", timeout=8.0)

        self.triggers = tuple(sorted(_DEFAULT_TRIGGERS, key=len, reverse=True))

        self._batches: Dict[str, _BatchSession] = {}
        self._egress_task: Optional[asyncio.Task] = None
        self._start_egress_log()
        logger.info(
            f"picfinder v{_REGISTER_VERSION} 已加载：Lens={self.pipeline.config.lens_backend}，"
            f"Yandex={self.pipeline.config.enable_yandex}，"
            f"SauceNAO={self.pipeline.config.enable_saucenao}，"
            f"TraceMoe={self.pipeline.config.enable_tracemoe}"
        )

    def _build_pipeline_config(self) -> PipelineConfig:
        cfg = self.config
        return PipelineConfig(
            lens_backend=_LENS_BACKEND,
            lens_safe_search=_LENS_SAFE_SEARCH,
            lens_headless=_BROWSER_HEADLESS,
            lens_profile_dir="",
            lens_browser_channel="",
            lens_include_visual=_LENS_INCLUDE_VISUAL,
            enable_yandex=True,
            yandex_family_filter=False,
            enable_saucenao=True,
            enable_ascii2d=True,
            ascii2d_bovw=True,
            saucenao_api_key=str(cfg.get("saucenao_api_key", "") or "").strip(),
            saucenao_threshold=_SAUCENAO_THRESHOLD,
            enable_tracemoe=True,
            verify_sources=True,
            max_results=_MAX_RESULTS,
            engine_timeout=_ENGINE_TIMEOUT,
            proxy=self.proxy_http_url,
            browser_executable_path="",
            browser_user_data_dir="",
            browser_timeout=float(_BROWSER_TIMEOUT),
        )

    def _setup_proxy(self) -> None:
        raw = str(self.config.get("proxy", "") or "").strip()
        self.proxy_service = None
        self.proxy_error = ""
        self.proxy_http_url = raw
        self.proxy_browser_url = raw.replace("socks5h://", "socks5://") if raw else ""
        self._proxy_task: Optional[asyncio.Task] = None
        if not raw:
            return
        if _is_proxy_url(raw):
            return
        try:
            self.proxy_service = ProxyService(raw)
            self.proxy_http_url = self.proxy_service.socks_url
            self.proxy_browser_url = self.proxy_service.browser_url
            logger.info(
                f"picfinder: 内置代理已配置 {self.proxy_service.socks_url}，"
                f"节点数={len(self.proxy_service.ss_uri.splitlines())}"
            )
        except Exception as exc:
            self.proxy_error = f"代理节点解析失败: {exc}"
            self.proxy_http_url = ""
            self.proxy_browser_url = ""
            logger.error(f"picfinder: {self.proxy_error}")
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None:
            self._proxy_task = loop.create_task(self.proxy_service.ensure_started())

    async def _ensure_tool_proxy(self) -> None:
        if self.tool_proxy_needs_service:
            await self._ensure_proxy()

    async def _ensure_proxy(self) -> None:
        if self.proxy_error:
            raise RuntimeError(self.proxy_error)
        if self.proxy_service is not None:
            if not await self.proxy_service.ensure_started():
                raise RuntimeError("内置代理启动失败，请检查节点/密码/加密方式")

    def _build_browser_config(self, proxy_url: Optional[str] = None, *, persist: bool = True) -> BrowserConfig:
        cfg = self.config
        user_data_dir = ""
        if persist:
            user_data_dir = str(cfg.get("browser_profile_dir", "") or "").strip()
            if user_data_dir:
                try:
                    os.makedirs(user_data_dir, exist_ok=True)
                except OSError as exc:
                    logger.warning(f"picfinder: 无法创建浏览器配置目录 {user_data_dir}: {exc}")
                    user_data_dir = ""
        return BrowserConfig(
            executable_path="",
            user_data_dir=user_data_dir,
            headless=_BROWSER_HEADLESS,
            proxy=self.proxy_browser_url if proxy_url is None else proxy_url,
            channel="",
            viewport_width=1280,
            viewport_height=900,
            navigation_timeout_ms=_BROWSER_TIMEOUT * 1000,
            action_timeout_ms=15000,
            allow_local_urls=_BROWSER_ALLOW_LOCAL,
            screenshot_dir="",
            auto_install_browser=_BROWSER_AUTO_INSTALL,
            stealth=True,
            cookie_file=self.cookie_file,
            timezone=_BROWSER_TIMEZONE,
        )

    # ------------------------------------------------------------------
    # 出口自检
    # ------------------------------------------------------------------
    def _start_egress_log(self) -> None:
        if self.proxy_service is None and not self.proxy_http_url:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._egress_task = loop.create_task(self._log_egress())

    async def _log_egress(self) -> None:
        """启动后确认一次「代理到底有没有生效」，把出口 IP 写进日志。"""
        try:
            if self.proxy_service is not None and not await self.proxy_service.ensure_started():
                logger.warning("picfinder: 内置代理没起来，会退回直连，检查 proxy 节点/密码")
                return
            client = HttpClient(proxy=self.proxy_http_url, timeout=8.0)
            try:
                via = await probe(client)
            finally:
                await client.close()
            direct = await probe(self.image_client) if self.image_client else None
        except Exception as exc:
            logger.debug(f"picfinder: 出口自检失败 {exc}")
            return
        if via.ok:
            extra = ""
            direct_text = "探测失败"
            if direct is not None and direct.ok:
                direct_text = direct.line()
                if direct.ip == via.ip:
                    extra = "（与直连同 IP，代理可能没生效）"
            logger.info(f"picfinder: 代理出口 {via.line()}｜直连 {direct_text}{extra}")
        else:
            logger.warning(f"picfinder: 代理出口探测失败: {via.error}")

    async def terminate(self) -> None:
        for key in list(self._batches):
            self._finish_batch(key)
        for page in list(self._tabs):
            try:
                await page.close()
            except Exception:
                pass
        if self.tool_browser is not self.browser:
            try:
                await self.tool_browser.close()
            except Exception:
                pass
        try:
            await self.pipeline.close()
        except Exception as exc:  # pragma: no cover
            logger.debug(f"picfinder: 关闭失败 {exc}")
        try:
            await self.image_client.close()
        except Exception:
            pass
        if self._egress_task and not self._egress_task.done():
            self._egress_task.cancel()
        if self._proxy_task and not self._proxy_task.done():
            self._proxy_task.cancel()
            try:
                await self._proxy_task
            except (asyncio.CancelledError, Exception):
                pass
        if self.proxy_service is not None:
            try:
                await self.proxy_service.stop()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # 原版触发规则
    # ------------------------------------------------------------------
    def _clean_text(self, event: AstrMessageEvent) -> str:
        return (event.message_str or "").strip().lstrip("/").strip()

    def _plain_text(self, event: AstrMessageEvent) -> str:
        """只取纯文本段，@ 段会被自然排除，用来判断「整条消息只有触发词」。"""
        parts = []
        for segment in event.get_messages():
            if isinstance(segment, Comp.Plain):
                parts.append(str(getattr(segment, "text", "") or ""))
        return "".join(parts)

    def _thanks_hit(self, text: str) -> bool:
        return "我都不看这些的喵" in text

    def _has_reply(self, event: AstrMessageEvent) -> bool:
        return any(isinstance(segment, Comp.Reply) for segment in event.get_messages())

    def _chat_key(self, event: AstrMessageEvent) -> str:
        # 两个 bot 同时在同一个群时互不干扰：会话按「适配器 + 群」分开，
        # 各自发自己的「发，快发喵」、各自数自己的图，不去重也不合并。
        chat = str(event.get_group_id() or event.get_sender_id() or "")
        platform = ""
        try:
            platform = str(event.get_platform_id() or "")
        except Exception:
            platform = ""
        if platform and chat:
            return f"{platform}|{chat}"
        return chat or str(event.unified_msg_origin or event.get_session_id())

    def _sender(self, event: AstrMessageEvent) -> str:
        return str(event.get_sender_id() or "")

    # ------------------------------------------------------------------
    # 消息入口
    # ------------------------------------------------------------------
    @filter.event_message_type(filter.EventMessageType.ALL, priority=100)
    async def on_message(self, event: AstrMessageEvent):
        text = self._clean_text(event)
        key = self._chat_key(event)
        uid = self._sender(event)
        session = self._batches.get(key)

        if self._thanks_hit(text):
            event.stop_event()
            async for result in self._handle_thanks(event, key, uid):
                yield result
            return

        keyword = keyword_hit(text, self.triggers)
        in_batch = bool(session and session.user_id == uid)
        if not keyword and not in_batch:
            return

        image_url, local_path = await resolve_event_image(self.context, event)
        has_image = bool(image_url or local_path)

        if has_image:
            if in_batch:
                async for result in self._handle_batch_image(event, key, session, image_url, local_path, uid):
                    yield result
                return
            if should_search(keyword=keyword, has_image=True):
                event.stop_event()
                async for result in self._search(event, image_url, local_path):
                    yield result
            return

        # 没有图：只有「整条消息就是触发词」才进入连续搜图模式
        bare = is_bare_keyword(self._plain_text(event), self.triggers) or is_bare_keyword(
            text, self.triggers
        )
        if keyword and bare:
            event.stop_event()
            async for result in self._handle_no_image(event, key, uid, session):
                yield result

    # ------------------------------------------------------------------
    # 连续搜图模式
    # ------------------------------------------------------------------
    async def _handle_no_image(self, event, key: str, uid: str, session: Optional[_BatchSession]):
        if self._has_reply(event):
            yield event.plain_result("未找到图片喵")
            return
        if session is not None:
            if session.user_id == uid:
                session.expires = time.monotonic() + self.batch_timeout
                yield event.plain_result("发，快发喵")
            else:
                yield event.chain_result([Comp.At(qq=session.user_id), Comp.Plain("正在搜图，请耐心等待喵~")])
            return

        session = _BatchSession(
            user_id=uid,
            expires=time.monotonic() + self.batch_timeout,
            count=0,
        )
        self._batches[key] = session
        session.task = asyncio.create_task(self._batch_timeout(key, event))
        yield event.plain_result("发，快发喵")

    async def _batch_timeout(self, key: str, event: AstrMessageEvent):
        try:
            await asyncio.sleep(self.batch_timeout)
            while key in self._batches:
                session = self._batches[key]
                remaining = session.expires - time.monotonic()
                if remaining > 0:
                    await asyncio.sleep(min(remaining, 5))
                    continue
                self._batches.pop(key, None)
                if session.count == 0:
                    await self._send_at(event, session.user_id, "我的图图呢喵？")
                else:
                    await event.send(event.plain_result(f"怎么才{session.count}张喵～"))
                return
        except asyncio.CancelledError:
            return
        except Exception as exc:  # pragma: no cover
            logger.debug(f"picfinder: 连续搜图计时器退出 {exc}")

    async def _send_at(self, event: AstrMessageEvent, uid: str, text: str) -> None:
        await event.send(event.chain_result([Comp.At(qq=uid), Comp.Plain(text)]))

    async def _handle_batch_image(self, event, key: str, session: _BatchSession, image_url, local_path, uid: str):
        session.count += 1
        session.expires = time.monotonic() + self.batch_timeout
        event.stop_event()
        async for result in self._search(event, image_url, local_path):
            yield result

    async def _handle_thanks(self, event, key: str, uid: str):
        session = self._batches.get(key)
        if session is None:
            yield event.plain_result("にゃ～")
            return
        if session.user_id != uid:
            yield event.plain_result("不能替别人结束搜图喵～")
            return
        self._finish_batch(key)
        if session.count == 0:
            yield event.plain_result("超时了喵，图图是被夹了喵？")
        else:
            yield event.plain_result(f"好喵，一共发了{session.count}张喵～")

    def _finish_batch(self, key: str) -> None:
        session = self._batches.pop(key, None)
        if session and session.task and not session.task.done():
            session.task.cancel()

    # ------------------------------------------------------------------
    # 检索与回复
    # ------------------------------------------------------------------
    async def _search(self, event: AstrMessageEvent, image_url: Optional[str], local_path: Optional[str]):
        try:
            await self._ensure_proxy()
        except Exception as exc:
            yield event.plain_result(f"代理启动失败：{exc}")
            return
        try:
            img = await self._load_image(image_url, local_path)
        except Exception as exc:
            logger.info(f"picfinder: 图片读取失败 {image_url or local_path}: {exc}")
            yield event.plain_result("未找到图片喵")
            return

        logger.info(f"picfinder: 开始识图 {img.width}x{img.height} dhash={img.dhash:016x}")
        try:
            report = await self.pipeline.search(img)
        except Exception as exc:  # pragma: no cover
            logger.error(f"picfinder: 检索失败 {exc}", exc_info=True)
            yield event.plain_result("呜呜呜，都检索失败喵，牡蛎~")
            return

        blocks = await build_legacy_blocks(
            report.candidates,
            self.pipeline.client,
            thumb_on=self.thumb_on,
            per_engine=self.result_num,
            engine_errors=self._errors_by_engine(report),
        )
        async for result in self._send_blocks(event, blocks):
            yield result

    async def _load_image(self, image_url: Optional[str], local_path: Optional[str]) -> ImageInput:
        """取图：直连和代理同时发，谁先成功用谁。

        QQ 图床在不同线路上快慢差很多，串行「先直连、失败再代理」会白等一整个超时。
        """
        if local_path:
            return await ImageInput.load(self.image_client, path=local_path)
        if not image_url:
            raise ValueError("缺少图片地址")

        started = time.monotonic()
        tasks = [asyncio.create_task(ImageInput.load(self.image_client, url=image_url))]
        if self.proxy_http_url:
            tasks.append(asyncio.create_task(ImageInput.load(self.pipeline.client, url=image_url)))
        pending = set(tasks)
        last_error: Optional[Exception] = None
        try:
            while pending:
                done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    try:
                        img = task.result()
                    except Exception as exc:
                        last_error = exc
                        continue
                    logger.info(
                        f"picfinder: 图片下载 {time.monotonic() - started:.1f}s "
                        f"{img.width}x{img.height}（{len(tasks)} 路并发取首个成功）"
                    )
                    return img
            raise last_error or ValueError("图片下载失败")
        finally:
            for task in pending:
                task.cancel()

    async def _browser_page(self) -> BrowserPage:
        await self._ensure_tool_proxy()
        if self._active is None or self._active.closed:
            page = await self.tool_browser.new_page()
            self._tabs.append(page)
            self._active = page
        return self._active

    async def _browser_text(self, page: BrowserPage, limit: int = 1200) -> str:
        title = await page.title()
        url = await page.url()
        text = await page.evaluate("() => document.body ? document.body.innerText : ''")
        excerpt = str(text or "")[:limit]
        return f"URL: {url}\nTitle: {title or '(none)'}\n\n{excerpt or '(no text)'}"

    @filter.llm_tool(name="browser_open")
    async def browser_open(self, event: AstrMessageEvent, url: str, new_tab: bool = False):
        """Open a URL in the controlled headless Chromium and wait for it to load.

        Args:
            url(string): Absolute URL, or a hostname with an implied https://.
            new_tab(boolean): Open in a new tab instead of navigating the active tab.
        """
        try:
            await self._ensure_tool_proxy()
            target = normalize_url(url, allow_local=self.tool_browser.config.allow_local_urls)
            async with self._browser_lock:
                if new_tab or self._active is None or self._active.closed:
                    page = await self.tool_browser.new_page(target)
                    self._tabs.append(page)
                    self._active = page
                else:
                    page = self._active
                    await page.goto(target)
                return await self._browser_text(page)
        except Exception as exc:
            return f"browser_open 失败: {_browser_message(exc)}"

    @filter.llm_tool(name="browser_snapshot")
    async def browser_snapshot(self, event: AstrMessageEvent, max_text: int = 12000, max_elements: int = 120):
        """Read the current page: URL, title, text, scroll position, and interactive elements with e<n> refs.

        Args:
            max_text(number): Maximum characters of page text to return (default 12000).
            max_elements(number): Maximum interactive elements to list (default 120).
        """
        try:
            async with self._browser_lock:
                page = await self._browser_page()
                snapshot = await page.snapshot(max_text=max_text, max_elements=max_elements)
                return format_snapshot(snapshot)
        except Exception as exc:
            return f"browser_snapshot 失败: {_browser_message(exc)}"

    @filter.llm_tool(name="browser_click")
    async def browser_click(self, event: AstrMessageEvent, ref: str = "", selector: str = "", text: str = ""):
        """Click an element by snapshot ref, CSS selector, or visible text.

        Args:
            ref(string): Element ref from browser_snapshot, such as "e12".
            selector(string): CSS selector, used when ref is empty.
            text(string): Visible text of the element, used when ref/selector are empty.
        """
        try:
            async with self._browser_lock:
                page = await self._browser_page()
                await page.click(ref=ref or None, selector=selector or None, text=text or None)
                return await self._browser_text(page, 800)
        except Exception as exc:
            return f"browser_click 失败: {_browser_message(exc)}"

    @filter.llm_tool(name="browser_type")
    async def browser_type(
        self,
        event: AstrMessageEvent,
        value: str,
        ref: str = "",
        selector: str = "",
        text: str = "",
        submit: bool = False,
    ):
        """Type text into a field, optionally pressing Enter.

        Args:
            value(string): Text to enter; an empty string clears the field.
            ref(string): Element ref from browser_snapshot.
            selector(string): CSS selector, used when ref is empty.
            text(string): Visible label of the field, used when ref/selector are empty.
            submit(boolean): Press Enter after typing.
        """
        try:
            async with self._browser_lock:
                page = await self._browser_page()
                await page.type_text(
                    value,
                    ref=ref or None,
                    selector=selector or None,
                    text=text or None,
                    submit=submit,
                )
                return await self._browser_text(page, 800)
        except Exception as exc:
            return f"browser_type 失败: {_browser_message(exc)}"

    @filter.llm_tool(name="browser_press")
    async def browser_press(self, event: AstrMessageEvent, key: str = "Enter"):
        """Press one keyboard key in the focused element.

        Args:
            key(string): Key name such as Enter, Tab, Escape, Backspace, ArrowDown, PageDown.
        """
        try:
            async with self._browser_lock:
                page = await self._browser_page()
                await page.press(key)
                return await self._browser_text(page, 800)
        except Exception as exc:
            return f"browser_press 失败: {_browser_message(exc)}"

    @filter.llm_tool(name="browser_scroll")
    async def browser_scroll(
        self, event: AstrMessageEvent, direction: str = "down", pages: float = 1.0, delta_y: int = 0
    ):
        """Scroll the current page.

        Args:
            direction(string): down or up.
            pages(number): Screenfuls to scroll when delta_y is 0.
            delta_y(number): Exact wheel delta in pixels; positive scrolls down.
        """
        try:
            amount = int(delta_y) if delta_y else int(pages * self.tool_browser.config.viewport_height)
            if direction.lower() == "up" and delta_y == 0:
                amount = -amount
            async with self._browser_lock:
                page = await self._browser_page()
                await page.scroll(delta_y=amount)
                position = await page.evaluate(
                    "() => ({ y: window.scrollY, "
                    "height: document.documentElement.scrollHeight, "
                    "viewport: window.innerHeight })"
                )
                return (
                    f"Scroll: y={position.get('y', 0)} of {position.get('height', 0)} "
                    f"(viewport {position.get('viewport', 0)})"
                )
        except Exception as exc:
            return f"browser_scroll 失败: {_browser_message(exc)}"

    @filter.llm_tool(name="browser_screenshot")
    async def browser_screenshot(self, event: AstrMessageEvent, full_page: bool = False, filename: str = ""):
        """Capture the current page as a PNG and send it to the chat, then return its path.

        The image goes straight to the chat, so do not call this on blank, error or
        not-yet-loaded pages: check browser_snapshot first, or the user gets a white picture.

        Args:
            full_page(boolean): Capture the entire scrollable page instead of the viewport.
            filename(string): Optional file name; saved under the configured screenshot directory.
        """
        try:
            async with self._browser_lock:
                page = await self._browser_page()
                stats = await page.content_stats()
                path = await page.screenshot(filename=filename or None, full_page=full_page)
                blank = is_blank_page(stats) or await _image_is_blank(path)
                if blank:
                    return (
                        "页面是空白的（没有正文也没有图片），截图已保存但没有发到群里"
                        f"（{path}）。先用 browser_snapshot 看看页面有没有加载出来。"
                    )
                try:
                    await event.send(event.image_result(path))
                except Exception:
                    pass
                return f"截图已保存并发送: {path}"
        except Exception as exc:
            return f"browser_screenshot 失败: {_browser_message(exc)}"

    @filter.llm_tool(name="browser_evaluate")
    async def browser_evaluate(self, event: AstrMessageEvent, expression: str):
        """Evaluate a JavaScript expression in the current page and return its JSON value.

        Args:
            expression(string): JavaScript expression, for example document.title.
        """
        try:
            async with self._browser_lock:
                page = await self._browser_page()
                value = await page.evaluate(expression)
                return evaluate_to_text(value)
        except Exception as exc:
            return f"browser_evaluate 失败: {_browser_message(exc)}"

    @filter.llm_tool(name="browser_tabs")
    async def browser_tabs(self, event: AstrMessageEvent, action: str = "list", index: int = -1):
        """List, create, select, or close browser tabs.

        Args:
            action(string): list, new, select, or close.
            index(number): Tab index from list; required for select/close.
        """
        try:
            async with self._browser_lock:
                action = (action or "list").lower()
                if action == "new":
                    page = await self.tool_browser.new_page()
                    self._tabs.append(page)
                    self._active = page
                elif action == "list":
                    pass
                elif action in ("select", "close"):
                    if index < 0 or index >= len(self._tabs):
                        raise BrowserError(f"标签页索引无效: {index}")
                    if action == "select":
                        self._active = self._tabs[index]
                    else:
                        page = self._tabs.pop(index)
                        await page.close()
                        if self._active is page:
                            self._active = self._tabs[-1] if self._tabs else None
                else:
                    raise BrowserError(f"未知 action: {action}")
                lines = []
                for i, page in enumerate(self._tabs):
                    url = await page.url()
                    title = await page.title()
                    mark = " *" if page is self._active else ""
                    lines.append(f"{i}: {title or '(none)'} — {url}{mark}")
                return "\\n".join(lines) or "(no tabs)"
        except Exception as exc:
            return f"browser_tabs 失败: {_browser_message(exc)}"

    @filter.llm_tool(name="browser_close")
    async def browser_close(self, event: AstrMessageEvent):
        """Close all browser tabs opened by these tools; the browser process is kept for reuse."""
        try:
            async with self._browser_lock:
                for page in list(self._tabs):
                    await page.close()
                self._tabs.clear()
                self._active = None
                return "已关闭全部标签页。"
        except Exception as exc:
            return f"browser_close 失败: {_browser_message(exc)}"

    @staticmethod
    def _errors_by_engine(report) -> dict:
        errors: Dict[str, str] = {}
        for item in report.errors:
            name, _, message = item.partition(": ")
            if name:
                errors[name] = message
        return errors

    def _components(self, block: LegacyBlock) -> list[Any]:
        components: list[Any] = []
        if block.thumb:
            try:
                components.append(Comp.Image.fromBytes(block.thumb))
            except Exception:
                components.append(Comp.Plain("[预览图下载失败]"))
        components.append(Comp.Plain(block.text))
        return components

    async def _send_blocks(self, event: AstrMessageEvent, blocks: list[LegacyBlock]):
        if not blocks:
            return
        if self.chain_reply and event.get_platform_name() == "aiocqhttp" and hasattr(Comp, "Nodes"):
            uin = str(event.get_self_id() or "0")
            nodes = [Comp.Node(content=self._components(block), name=self.bot_nickname, uin=uin) for block in blocks]
            yield event.chain_result([Comp.Nodes(nodes=nodes)])
            return
        for block in blocks:
            yield event.chain_result(self._components(block))


async def _image_is_blank(path: str) -> bool:
    """截图文件本身是不是白板（页面加载失败时客户端只能截到一片白）。"""
    try:
        data = await asyncio.to_thread(lambda: open(path, "rb").read())
    except Exception:
        return False
    return looks_blank(data)


def _find_cookie_file() -> str:
    """插件目录下的 data/cookies.txt|json：用户自己粘贴的浏览器 Cookie。"""
    for name in _COOKIE_FILES:
        path = os.path.join(_PLUGIN_DIR, "data", name)
        if os.path.isfile(path):
            return path
    return ""


def _is_proxy_url(raw: str) -> bool:
    lowered = raw.lower()
    if "\n" in raw or "type:" in lowered or lowered.startswith(("- ", "-{")):
        return False
    return lowered.startswith(("http://", "https://", "socks4://", "socks5://", "socks5h://"))


def _browser_message(exc: Exception) -> str:
    text = str(exc)
    return text[:500] if text else exc.__class__.__name__


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
