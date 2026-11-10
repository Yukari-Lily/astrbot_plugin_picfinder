"""AI 增强多引擎全网识图插件 (astrbot_plugin_picfinder)。
优先以 Google Lens 视觉完全匹配检索全网 X (Twitter)、Bilibili、YouTube、Pixiv 等多平台出处；
融合 SauceNAO 高精度二次元图库与 TraceMoe 动漫截图识别；
通过 AstrBot 原生 Provider 多模态大模型进行画面解析与智能交叉提炼，并提供无果兜底。
"""

import asyncio
import os
import re
import tempfile
import time
from typing import Any, Dict, List, Optional, Tuple

import httpx

import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

from .ai import ImageAnalysisResult, MultimodalAnalyzer
from .engines import (
    Ascii2DEngine,
    EngineSearchResult,
    GoogleLensEngine,
    SauceNAOEngine,
    SearchResultItem,
    TraceMoeEngine,
    WebSearchEngine,
)
from .message_utils import resolve_event_image
from .normalizer import (
    ArtworkSample,
    AuthorArtworksManager,
    AuthorProfile,
    extract_platform_sources_from_text,
    normalize_platform_url,
)


@register(
    "astrbot_plugin_picfinder",
    "Yukari Lily",
    "AI 增强多引擎全网识图",
    "1.2.0",
    "https://github.com/Yukari-Lily/astrbot_plugin_picfinder",
)
class PicFinderPlugin(Star):
    """AstrBot 多引擎 AI 搜图插件"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

        # 读取配置项
        self.prioritize_google_lens = bool(config.get("prioritize_google_lens", True))
        self.enable_ai_enhancement = bool(config.get("enable_ai_enhancement", True))
        self.ai_fallback_search = bool(config.get("ai_fallback_search", True))
        self.saucenao_api_key = str(config.get("saucenao_api_key", "") or "").strip()
        self.saucenao_threshold = float(config.get("saucenao_threshold", 70.0))
        self.enable_ascii2d = bool(config.get("enable_ascii2d", True))
        self.enable_tracemoe = bool(config.get("enable_tracemoe", True))
        self.proxy = str(config.get("proxy", "") or "").strip()
        # Ascii2D 专用反 403 配置（留空则跟随全局 proxy）
        self.ascii2d_proxy = str(config.get("ascii2d_proxy", "") or "").strip()
        self.ascii2d_ss_uri = str(config.get("ascii2d_ss_uri", "") or "").strip()
        self.ascii2d_ss_node = str(config.get("ascii2d_ss_node", "") or "").strip()
        self.ascii2d_cookie = str(config.get("ascii2d_cookie", "") or "").strip()
        self.ascii2d_browser_fallback = bool(config.get("ascii2d_browser_fallback", True))
        self.ascii2d_bovw = bool(config.get("ascii2d_bovw", True))
        self.max_results = int(config.get("max_results", 3))
        self.show_thumbnails = bool(config.get("show_thumbnails", True))

        # 触发词 (仅保留识图)
        raw_triggers = config.get("triggers") or ["识图"]
        if isinstance(raw_triggers, (list, tuple)):
            self.triggers = tuple(sorted({str(t).strip() for t in raw_triggers if str(t).strip()}, key=len, reverse=True))
        else:
            self.triggers = ("识图",)

        # 初始化引擎
        self.lens_engine = GoogleLensEngine(proxy=self.proxy)
        self.sauce_engine = SauceNAOEngine(
            api_key=self.saucenao_api_key,
            threshold=self.saucenao_threshold,
            proxy=self.proxy,
        )
        self.ascii_engine = Ascii2DEngine(
            proxy=self.ascii2d_proxy or self.proxy,
            cookie=self.ascii2d_cookie,
            bovw=self.ascii2d_bovw,
            browser_fallback=self.ascii2d_browser_fallback,
            ss_uri=self.ascii2d_ss_uri,
            ss_node=self.ascii2d_ss_node,
        )
        self.tracemoe_engine = TraceMoeEngine(proxy=self.proxy)
        self.web_search = WebSearchEngine(proxy=self.proxy)
        self.ai_analyzer = MultimodalAnalyzer(self.context)
        self.author_manager = AuthorArtworksManager(proxy=self.proxy)
        self._recent_msg: Dict[str, float] = {}

    async def terminate(self) -> None:
        """插件停用：关掉内置 Shadowsocks 代理监听口，别留野端口。"""
        try:
            await self.ascii_engine.aclose()
        except Exception as exc:  # pragma: no cover - 停用阶段尽力而为
            logger.debug(f"关闭 Ascii2D 引擎失败: {exc}")

    def _message_key(self, event: AstrMessageEvent) -> Optional[str]:
        mid = getattr(getattr(event, "message_obj", None), "message_id", None)
        if mid is None:
            return None
        return f"{event.unified_msg_origin}|{mid}"

    def _dedup_hit(self, event: AstrMessageEvent) -> bool:
        """多 Bot 实例或重投时去重"""
        key = self._message_key(event)
        if key is None:
            return False
        now = time.monotonic()
        for k, ts in list(self._recent_msg.items()):
            if now - ts > 20.0:
                self._recent_msg.pop(k, None)
        if key in self._recent_msg:
            return True
        self._recent_msg[key] = now
        return False

    def _match_trigger(self, event: AstrMessageEvent) -> bool:
        """检查消息是否命中识图触发词 (支持前缀、后缀或单独发送)"""
        text = (event.message_str or "").strip()
        if not text:
            return False
        clean = text.lstrip("/").strip()
        for t in self.triggers:
            if clean == t:
                return True
            if clean.startswith(t):
                return True
            if clean.endswith(t):
                return True
            if t in clean:
                return True
        return False

    # ----------------------------------------------------------------------
    # 消息指令与事件监听 (支持无前缀、后缀、回复及斜杠指令)
    # ----------------------------------------------------------------------

    @filter.event_message_type(filter.EventMessageType.ALL, priority=100)
    async def on_message(self, event: AstrMessageEvent):
        """监听所有群聊与私聊消息，支持「识图 [图片]」、「[图片] 识图」及「回复图片 识图」"""
        if not self._match_trigger(event):
            return

        image_url, local_path = await resolve_event_image(self.context, event)
        if not image_url and not local_path:
            # 仅当用户明确只发了 "识图" 时才提示，避免群内闲聊误触
            clean_text = (event.message_str or "").strip().lstrip("/").strip()
            if clean_text in self.triggers:
                event.stop_event()
                yield event.plain_result("请在发送指令时附带图片，或回复包含图片的消息发送「识图」喵～")
            return

        if self._dedup_hit(event):
            return

        event.stop_event()
        logger.info(f"picfinder: 触发识图: image_url={image_url}")
        async for res in self._process_image_search(event, image_url, local_path):
            yield res

    @filter.command("识图")
    async def cmd_search_image(self, event: AstrMessageEvent):
        """兼容斜杠命令触发 (/识图)"""
        if self._dedup_hit(event):
            return

        image_url, local_path = await resolve_event_image(self.context, event)
        if not image_url and not local_path:
            yield event.plain_result("请在发送指令时附带图片，或回复包含图片的消息发送「识图」喵～")
            return

        event.stop_event()
        logger.info(f"picfinder: 触发斜杠识图: image_url={image_url}")
        async for res in self._process_image_search(event, image_url, local_path):
            yield res

    # ----------------------------------------------------------------------
    # 聊天模型可调用的函数工具 (Function Calling Tools)
    # ----------------------------------------------------------------------

    @filter.llm_tool(name="extract_platform_source")
    async def extract_platform_source(self, event: AstrMessageEvent, url: str):
        """深度解析并提取互联网平台链接（X/Twitter、哔哩哔哩、YouTube、Pixiv 等）的出处、作者、ID与规范化原址。

        Args:
            url(string): 需要解析的互联网链接或包含链接的文本（如 X/Twitter 推文、B站视频/动态、YouTube 视频、Pixiv 画作链接）
        """
        sources = extract_platform_sources_from_text(url)
        if not sources:
            yield event.plain_result(f"未能从输入中识别出有效的互联网平台链接: {url}")
            return

        lines = [f"已成功深度解析出 {len(sources)} 个平台来源信息："]
        for idx, s in enumerate(sources, 1):
            lines.append(f"{idx}. {s.badge} 平台: {s.platform_name}")
            if s.author:
                lines.append(f"   - 作者/UP主: @{s.author}")
            if s.item_id:
                lines.append(f"   - 资源ID/BV号: {s.item_id}")
            lines.append(f"   - 规范化出处原链: {s.canonical_url}")

        yield event.plain_result("\n".join(lines))

    @filter.llm_tool(name="search_image_by_url")
    async def search_image_by_url(self, event: AstrMessageEvent, image_url: str):
        """对指定的公开图片直链 URL 执行全网反向以图搜图（调用 Google Lens、SauceNAO、TraceMoe），返回原始出处和全网平台链接。

        Args:
            image_url(string): 需要进行以图搜图的公开图片直链 URL
        """
        clean_url = image_url.strip()
        if not clean_url.startswith("http"):
            yield event.plain_result("输入的 image_url 必须是以 http:// 或 https:// 开头的有效图片链接。")
            return

        lens_task = self.lens_engine.search(clean_url, max_results=self.max_results)
        sauce_task = self.sauce_engine.search(clean_url, max_results=self.max_results)
        trace_task = (
            self.tracemoe_engine.search(clean_url, max_results=1)
            if self.enable_tracemoe
            else asyncio.sleep(0, result=EngineSearchResult("TraceMoe"))
        )

        lens_res, sauce_res, trace_res = await asyncio.gather(lens_task, sauce_task, trace_task)

        lines = [f"已完成对图片「{clean_url}」的全网以图搜图："]
        found_any = False

        if lens_res and lens_res.has_results:
            found_any = True
            lines.append("\n🔍 【Google Lens 全网视觉匹配】")
            for item in lens_res.items[: self.max_results]:
                badge = item.extra_info.get("badge") or f"[{item.platform}]"
                author_str = f" @{item.author}" if item.author else ""
                title_str = f" 「{item.title}」" if item.title else ""
                lines.append(f"- {badge}{author_str}{title_str} -> {item.url}")

        if sauce_res and sauce_res.has_results:
            found_any = True
            lines.append(f"\n🎨 【SauceNAO 二次元图库】(最高相似度: {sauce_res.best_similarity:.1f}%)")
            for item in sauce_res.items[: self.max_results]:
                badge = item.extra_info.get("badge") or f"[{item.platform}]"
                author_str = f" 画师: {item.author}" if item.author else ""
                title_str = f" 「{item.title}」" if item.title else ""
                lines.append(f"- {badge} [{item.similarity:.1f}%]{author_str}{title_str} -> {item.url}")

        if trace_res and trace_res.has_results:
            found_any = True
            lines.append("\n🎬 【TraceMoe 动漫画面识别】")
            for item in trace_res.items:
                lines.append(f"- 📺 {item.title} (相似度: {item.similarity:.1f}%) -> {item.url}")

        if not found_any:
            lines.append("\n未能从各搜索引擎找到完全匹配的公开来源。")

        yield event.plain_result("\n".join(lines))

    @filter.llm_tool(name="get_author_and_artworks_from_image")
    async def get_author_and_artworks_from_image(self, event: AstrMessageEvent, image_url: str):
        """从图片中识别平台作者/画师（如 X/Twitter、Pixiv、B站、Danbooru 等），并检索获取该作者的平台主页、相册作品集以及其他插画与代表作内容。

        Args:
            image_url(string): 需要识别画师并检索其更多作品的图片网络 URL
        """
        clean_url = image_url.strip()
        if not clean_url.startswith("http"):
            yield event.plain_result("输入的 image_url 必须是以 http:// 或 https:// 开头的有效图片链接。")
            return

        # 1. 执行多引擎搜图与 AI 视觉分析
        lens_task = self.lens_engine.search(clean_url, max_results=self.max_results)
        sauce_task = self.sauce_engine.search(clean_url, max_results=self.max_results)
        ai_task = (
            self.ai_analyzer.analyze_image(clean_url, umo=event.unified_msg_origin)
            if self.enable_ai_enhancement
            else asyncio.sleep(0, result=None)
        )

        lens_res, sauce_res, ai_analysis = await asyncio.gather(lens_task, sauce_task, ai_task)
        engine_results = [lens_res, sauce_res]

        # 如果常规搜图未识别出作者，尝试由 AI 联网检索补充
        if ai_analysis and not any(it.author for it in lens_res.items) and sauce_res.best_similarity < self.saucenao_threshold:
            queries = self.web_search.generate_queries_from_analysis(ai_analysis)
            if queries:
                web_res = await self.web_search.search_by_queries(queries, max_items=4)
                if web_res and web_res.has_results:
                    engine_results.append(web_res)

        # 2. 综合提取画师画像
        profile = self.author_manager.extract_author_profile(engine_results, ai_analysis=ai_analysis)

        # 3. 检索该作者的其他作品直链与样本
        await self.author_manager.fetch_author_other_artworks(profile, limit=5)

        # 4. 生成面向大模型的完整报告
        summary_text = self.author_manager.format_author_summary(profile)
        yield event.plain_result(summary_text)

    @filter.llm_tool(name="search_web_by_keywords")
    async def search_web_by_keywords(self, event: AstrMessageEvent, keywords: str):
        """当以图搜图未找到原图时，使用画面关键词、作品、角色名、水印文字或画师圈名，在全网（B站、X、微博、米游社、抖音等）进行实时联网检索并返回出处链接与动态。

        Args:
            keywords(string): 需要检索的关键词，如 "星穹铁道 姬子 cos 小黑" 或 "远坂凛 cos 小蓝"
        """
        clean_kw = keywords.strip()
        if not clean_kw:
            yield event.plain_result("检索关键词不能为空喵～")
            return

        res = await self.web_search.search_ddg(clean_kw, limit=self.max_results)
        if not res:
            yield event.plain_result(f"全网联网检索未找到与「{clean_kw}」相关的有效结果。")
            return

        lines = [f"已为您联网检索到关于「{clean_kw}」的以下出处："]
        for idx, it in enumerate(res, 1):
            badge = it.extra_info.get("badge") or f"[{it.platform}]"
            author_str = f" @{it.author}" if it.author else ""
            snippet_str = f"\n   📝 {it.extra_info.get('snippet', '')[:80]}..." if it.extra_info.get("snippet") else ""
            lines.append(f"{idx}. {badge} 「{it.title}」{author_str}\n   🔗 {it.url}{snippet_str}")

        yield event.plain_result("\n".join(lines))

    @filter.llm_tool(name="get_author_artworks_by_name")
    async def get_author_artworks_by_name(self, event: AstrMessageEvent, author_name: str):
        """根据画师/作者名称或推特 Handle，直接查询该作者的平台主页、相册与代表插画列表。

        Args:
            author_name(string): 画师的姓名、圈名、Pixiv 名或推特 Handle（如 米山舞、lack、morikura_en 等）
        """
        name_clean = author_name.strip()
        profile = AuthorProfile(name=name_clean, danbooru_tag=name_clean.lower().replace(" ", "_"))
        await self.author_manager.fetch_author_other_artworks(profile, limit=6)
        summary_text = self.author_manager.format_author_summary(profile)
        yield event.plain_result(summary_text)

    # ----------------------------------------------------------------------
    # 核心检索调度与 AI 增强流程
    # ----------------------------------------------------------------------

    async def _process_image_search(
        self,
        event: AstrMessageEvent,
        image_url: Optional[str],
        local_path: Optional[str],
    ):
        """多引擎并发搜索并交由 AI 综合提炼"""
        umo = getattr(event, "unified_msg_origin", None)

        # 如果只有本地文件路径，尝试获取其 URL 或直接搜索
        target_url = image_url or ""
        temp_file_to_clean: Optional[str] = None

        # 如果只有网络图片且需要本地文件备用，可异步下载微型临时文件
        if target_url and not local_path:
            local_path = await self._download_temp_image(target_url)
            if local_path:
                temp_file_to_clean = local_path

        try:
            # 1. 并发调度各引擎与 AI 画面解析
            tasks = []
            # 引擎 1: Google Lens (最高优先级)
            tasks.append(self.lens_engine.search(target_url, local_path=local_path, max_results=self.max_results))
            # 引擎 2: SauceNAO (二次元精准度)
            tasks.append(self.sauce_engine.search(target_url, local_path=local_path, max_results=self.max_results))

            # 引擎 3: TraceMoe (动漫识别，可选)
            if self.enable_tracemoe and target_url:
                tasks.append(self.tracemoe_engine.search(target_url, local_path=local_path, max_results=1))
            else:
                tasks.append(asyncio.sleep(0, result=EngineSearchResult("TraceMoe")))

            # 任务 4: AI 多模态预解析 (识别角色/作品/台词)
            img_ref = target_url or local_path or ""
            if self.enable_ai_enhancement and img_ref:
                tasks.append(self.ai_analyzer.analyze_image(img_ref, umo=umo))
            else:
                tasks.append(asyncio.sleep(0, result=None))

            # 并发执行
            lens_res, sauce_res, trace_res, ai_analysis = await asyncio.gather(*tasks)

            engine_results: List[EngineSearchResult] = [lens_res, sauce_res]
            if trace_res and trace_res.has_results:
                engine_results.append(trace_res)

            # 2. Ascii2D 兜底判断 (当 SauceNAO 相似度不足且启用时)
            if (
                self.enable_ascii2d
                and sauce_res.best_similarity < self.saucenao_threshold
                and not any(it.is_exact_match for it in lens_res.items)
            ):
                logger.info("SauceNAO 相似度不足且无完全匹配，启动 Ascii2D 补充检索...")
                ascii_res = await self.ascii_engine.search(target_url, local_path=local_path, max_results=2)
                if ascii_res and ascii_res.has_results:
                    engine_results.append(ascii_res)
                elif ascii_res and ascii_res.error:
                    logger.info(f"Ascii2D 本次跳过: {ascii_res.error}")

            # 3. AI 全网自动联网检索出处 (当常规搜图引擎未找到高确信度原图时)
            has_exact = any(it.is_exact_match for it in lens_res.items)
            has_high_sim = sauce_res.best_similarity >= self.saucenao_threshold
            if not (has_exact or has_high_sim) and self.ai_fallback_search and ai_analysis:
                queries = self.web_search.generate_queries_from_analysis(ai_analysis)
                if queries:
                    logger.info(f"以图搜图未找到完全匹配，AI 自动全网实时检索关键词: {queries}")
                    web_res = await self.web_search.search_by_queries(queries, max_items=self.max_results)
                    if web_res and web_res.has_results:
                        logger.info(f"AI 全网联网检索成功命中 {len(web_res.items)} 条候选出处")
                        engine_results.append(web_res)

            # 4. 提取画师与相册信息
            author_profile = self.author_manager.extract_author_profile(engine_results, ai_analysis=ai_analysis)

            # 5. AI 智能交叉验证与提炼
            ai_summary: Optional[str] = None
            if self.enable_ai_enhancement:
                ai_summary = await self.ai_analyzer.synthesize_results(
                    analysis=ai_analysis,
                    engine_results=engine_results,
                    umo=umo,
                )

            # 6. 组装返回消息链
            message_components = self._build_result_chain(
                engine_results=engine_results,
                ai_summary=ai_summary,
                ai_analysis=ai_analysis,
                author_profile=author_profile,
            )

            yield event.chain_result(message_components)

        finally:
            # 清理临时下载文件
            if temp_file_to_clean and os.path.exists(temp_file_to_clean):
                try:
                    os.remove(temp_file_to_clean)
                except OSError:
                    pass

    # ----------------------------------------------------------------------
    # 消息链构建器
    # ----------------------------------------------------------------------

    def _build_result_chain(
        self,
        engine_results: List[EngineSearchResult],
        ai_summary: Optional[str],
        ai_analysis: Optional[ImageAnalysisResult],
        author_profile: Optional[AuthorProfile] = None,
    ) -> List[Any]:
        """格式化输出消息链，组合文字总结、缩略图与高确信度来源链接"""
        components: List[Any] = []
        text_lines: List[str] = []

        # 1. 顶部 AI 智能提炼结论
        if ai_summary:
            text_lines.append("🤖 【AI 溯源与识别分析】")
            text_lines.append(ai_summary)
            text_lines.append("────────────────")

        # 2. 作者相册与个人主页直达
        if author_profile and (author_profile.gallery_url or author_profile.profile_url):
            name_disp = author_profile.name or author_profile.handle or "画师"
            lines_prof = [f"🎨 【创作者/画师出处】：{name_disp} ({author_profile.primary_platform})"]
            if author_profile.gallery_url:
                lines_prof.append(f"• 媒体相册/作品集: {author_profile.gallery_url}")
            elif author_profile.profile_url:
                lines_prof.append(f"• 平台个人主页: {author_profile.profile_url}")
            text_lines.append("\n".join(lines_prof))
            text_lines.append("")

        # 2. Google Lens 优先完全匹配与跨平台来源
        lens_result = next((er for er in engine_results if er.engine_name == "Google Lens"), None)
        if lens_result and lens_result.has_results:
            text_lines.append("🔍 【Google Lens 全网视觉匹配】")
            for item in lens_result.items[: self.max_results]:
                badge = item.extra_info.get("badge") or f"[{item.platform}]"
                author_part = f" @{item.author}" if item.author else ""
                title_part = f" 「{item.title}」" if item.title else ""
                text_lines.append(f"{badge}{author_part}{title_part}\n🔗 {item.url}")
            text_lines.append("")

        # 3. SauceNAO 二次元图库 (Pixiv / 画师 / 相似度)
        sauce_result = next((er for er in engine_results if er.engine_name == "SauceNAO"), None)
        if sauce_result and sauce_result.has_results:
            text_lines.append(f"🎨 【SauceNAO 精准二次元】(最高相似度: {sauce_result.best_similarity:.1f}%)")
            for item in sauce_result.items[: self.max_results]:
                badge = item.extra_info.get("badge") or f"[{item.platform}]"
                author_part = f" 画师: {item.author}" if item.author else ""
                title_part = f" 「{item.title}」" if item.title else ""
                sim_part = f" [{item.similarity:.1f}%]"
                text_lines.append(f"{badge}{sim_part}{author_part}{title_part}\n🔗 {item.url}")
            text_lines.append("")

        # 4. TraceMoe 动漫番剧匹配
        trace_result = next((er for er in engine_results if er.engine_name == "TraceMoe"), None)
        if trace_result and trace_result.has_results:
            text_lines.append("🎬 【TraceMoe 动漫画面匹配】")
            for item in trace_result.items:
                text_lines.append(f"📺 {item.title}\n相似度: {item.similarity:.1f}%\n🔗 预览: {item.url}")
            text_lines.append("")

        # 5. Ascii2D 检索结果 (若触发)
        ascii_result = next((er for er in engine_results if er.engine_name == "Ascii2D"), None)
        if ascii_result and ascii_result.has_results:
            text_lines.append("⛩️ 【Ascii2D 兜底检索】")
            for item in ascii_result.items[: self.max_results]:
                badge = item.extra_info.get("badge") or f"[{item.platform}]"
                author_part = f" 作者: {item.author}" if item.author else ""
                title_part = f" 「{item.title}」" if item.title else ""
                text_lines.append(f"{badge}{author_part}{title_part}\n🔗 {item.url}")
            text_lines.append("")

        # 6. AI 自动全网联网检索结果 (出处/动态/视频)
        web_result = next((er for er in engine_results if er.engine_name == "AI 联网检索"), None)
        if web_result and web_result.has_results:
            text_lines.append("🌐 【AI 自动全网检索出处】")
            for item in web_result.items[: self.max_results]:
                badge = item.extra_info.get("badge") or f"[{item.platform}]"
                author_part = f" @{item.author}" if item.author else ""
                title_part = f" 「{item.title}」" if item.title else ""
                text_lines.append(f"{badge}{author_part}{title_part}\n🔗 {item.url}")
            text_lines.append("")

        # 如果所有引擎均无结果
        if not any(er.has_results for er in engine_results) and not ai_summary:
            if ai_analysis and self.ai_fallback_search:
                chars = "、".join(ai_analysis.characters) if ai_analysis.characters else "未知"
                text_lines.append("⚠️ 【以图搜图未找到完全相同的原图】")
                text_lines.append(f"💡 经 AI 视觉特征分析推测：")
                text_lines.append(f"• 角色/实体: {chars}")
                text_lines.append(f"• 所属作品: {ai_analysis.origin_work or '未知'}")
                if ai_analysis.search_keywords:
                    text_lines.append(f"• 建议搜索关键词: {' / '.join(ai_analysis.search_keywords)}")
            else:
                text_lines.append("呜呜，所有搜图引擎都未能找到匹配的原图喵～")

        # 拼接纯文本部分
        final_text = "\n".join(text_lines).strip()
        components.append(Comp.Plain(final_text))

        # 附带缩略图预览 (如果开启且有可用缩略图)
        if self.show_thumbnails:
            best_thumb: Optional[str] = None
            # 优先从 Lens 或 SauceNAO 获取可用缩略图
            for er in engine_results:
                for it in er.items:
                    if it.thumbnail and it.thumbnail.startswith("http"):
                        best_thumb = it.thumbnail
                        break
                if best_thumb:
                    break

            if best_thumb:
                try:
                    components.append(Comp.Image.fromURL(best_thumb))
                except Exception as e:
                    logger.debug(f"添加缩略图失败跳过: {e}")

        return components

    # ----------------------------------------------------------------------
    # 辅助工具函数
    # ----------------------------------------------------------------------

    async def _download_temp_image(self, url: str) -> Optional[str]:
        """下载网络图片至临时目录供本地文件上传"""
        try:
            mounts = {}
            if self.proxy:
                mounts = {"http://": httpx.AsyncHTTPTransport(proxy=self.proxy), "https://": httpx.AsyncHTTPTransport(proxy=self.proxy)}

            async with httpx.AsyncClient(mounts=mounts, timeout=10.0) as client:
                resp = await client.get(url)
                if resp.status_code == 200:
                    ext = ".jpg"
                    if "image/png" in resp.headers.get("Content-Type", ""):
                        ext = ".png"
                    fd, path = tempfile.mkstemp(suffix=ext, prefix="picfinder_")
                    with os.fdopen(fd, "wb") as f:
                        f.write(resp.content)
                    return path
        except Exception as e:
            logger.debug(f"下载临时图片失败跳过: {e}")
        return None
