"""基于 AstrBot 多模态 Provider 的 AI 视觉分析与全网搜图结果提炼器。"""

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

try:
    from astrbot.api import logger
except ImportError:
    import logging
    logger = logging.getLogger("astrbot_plugin_picfinder.ai")

try:
    from ..engines.base import EngineSearchResult, SearchResultItem
except (ImportError, ValueError):
    from engines.base import EngineSearchResult, SearchResultItem


@dataclass
class ImageAnalysisResult:
    """多模态视觉大模型对画面的预解析结果"""
    subject_type: str = ""  # 二次元插画 / 动漫截图 / 游戏CG / 视频片段 / 真人等
    characters: List[str] = field(default_factory=list)  # 识别出的角色
    origin_work: str = ""  # 作品名 (IP)
    artist_or_author: str = ""  # 推测作者 / 画师 / Coser
    ocr_text: str = ""  # 水印、台词、文字、账号ID
    search_keywords: List[str] = field(default_factory=list)  # 推荐搜索词


class MultimodalAnalyzer:
    """AI 画面解析与结果智能总结引擎"""

    def __init__(self, context: Any):
        self.context = context

    async def analyze_image(self, image_url_or_path: str, umo: Any = None) -> Optional[ImageAnalysisResult]:
        """通过 AstrBot Provider 调用多模态大模型分析画面内容"""
        try:
            prov = await self.context.get_using_provider_async(umo=umo)
            if not prov:
                logger.debug("MultimodalAnalyzer: 未找到可用的 Provider")
                return None

            prompt = (
                "你是一个顶尖的图像溯源与二次元/全网视觉识别专家。请细致观察该图片，识别其核心实体、角色、作品和文字。\n"
                "特别注意画面四角、边缘或任意位置的水印、LOGO、Cos/摄影作者ID、抖音号、微博名或推特Handle。\n"
                "请直接严格输出如下标准的 JSON 字典（不要添加任何多余文字或前后缀）：\n"
                "{\n"
                '  "subject_type": "二次元插画/动漫截图/游戏CG/真人/梗图/视频片段",\n'
                '  "characters": ["角色名 (中/日/英)"],\n'
                '  "origin_work": "所属作品名 (如《原神》/《Fate》/《崩坏：星穹铁道》等)",\n'
                '  "artist_or_author": "画师水印/签名/作者名推测(如有)",\n'
                '  "ocr_text": "画面内清晰可见的水印、台词字幕、LOGO或文字",\n'
                '  "search_keywords": ["精炼检索词(如: 角色名 水印作者 cos)", "备用检索词"]\n'
                "}"
            )

            # 调用 AstrBot 多模态接口
            resp = await prov.text_chat(
                prompt=prompt,
                image_urls=[image_url_or_path],
            )

            if not resp or not hasattr(resp, "completion_text"):
                return None

            text = resp.completion_text.strip()
            # 清理可能的 markdown 代码块 ```json
            if text.startswith("```"):
                text = re.sub(r"^```(?:json)?\s*", "", text)
                text = re.sub(r"\s*```$", "", text)

            data = json.loads(text)
            return ImageAnalysisResult(
                subject_type=data.get("subject_type", ""),
                characters=data.get("characters") if isinstance(data.get("characters"), list) else [],
                origin_work=data.get("origin_work", ""),
                artist_or_author=data.get("artist_or_author", ""),
                ocr_text=data.get("ocr_text", ""),
                search_keywords=data.get("search_keywords") if isinstance(data.get("search_keywords"), list) else [],
            )

        except Exception as e:
            logger.debug(f"MultimodalAnalyzer: 视觉大模型解析跳过 ({e})")
            return None

    async def synthesize_results(
        self,
        analysis: Optional[ImageAnalysisResult],
        engine_results: List[EngineSearchResult],
        umo: Any = None,
    ) -> Optional[str]:
        """将视觉分析与多个搜索引擎及全网联网检索的结果交给 LLM 进行甄别与提炼"""
        try:
            prov = await self.context.get_using_provider_async(umo=umo)
            if not prov:
                return None

            # 汇总各引擎条目
            candidates_text_list = []
            for er in engine_results:
                if not er.items:
                    continue
                candidates_text_list.append(f"【搜索引擎 / 联网检索: {er.engine_name}】")
                for idx, it in enumerate(er.items[:4], 1):
                    author_part = f" | 作者: @{it.author}" if it.author else ""
                    snippet = f" | 摘要: {it.extra_info.get('snippet', '')[:60]}" if it.extra_info.get('snippet') else ""
                    candidates_text_list.append(
                        f"  {idx}. [{it.platform}] 标题: {it.title}{author_part} | 相似度/置信度: {it.similarity:.1f}% | 链接: {it.url}{snippet}"
                    )

            if not candidates_text_list and not analysis:
                return None

            analysis_summary = "无（未启用视觉分析）"
            if analysis:
                chars_str = "、".join(analysis.characters) if analysis.characters else "未知"
                analysis_summary = (
                    f"画面类型: {analysis.subject_type}\n"
                    f"识别角色: {chars_str}\n"
                    f"所属作品: {analysis.origin_work or '未知'}\n"
                    f"可见水印/文字: {analysis.ocr_text or '无'}\n"
                    f"作者签名推测: {analysis.artist_or_author or '无'}"
                )

            candidates_text = "\n".join(candidates_text_list) if candidates_text_list else "各搜图引擎均未检索到原图条目。"

            prompt = (
                "你是一个顶尖的图像溯源与全网跨平台多模态分析助手。\n"
                "以下是搜图引擎（Google Lens、SauceNAO等）以及系统根据画面水印/特征自动全网联网搜索到的候选出处与 AI 画面视觉特征：\n\n"
                f"### AI 画面特征预识别：\n{analysis_summary}\n\n"
                f"### 搜图引擎与全网联网检索到的候选来源列表：\n{candidates_text}\n\n"
                "请你对上述信息进行交叉比对与权威提炼，严格遵循以下原则：\n"
                "1. 优先剔除低质量壁纸爬虫站（如 Pinterest 等），从候选列表中甄别出最可信的【首发/原始作者出处】（特别是 X/Twitter 原推、Pixiv 插画、B站/抖音视频、米游社或微博动态出处）。\n"
                "2. 如果候选列表中有条目（包括搜图引擎结果或 AI 自动联网检索命中的动态/视频/帖子），请按以下格式简明、漂亮地呈现：\n"
                "🎯 **识别结论**：【作品名】/【角色名】(形态说明，如个人Cosplay/同人插画)\n"
                "👑 **推荐首发/出处**：[平台名] 作者/UP主/标题 -> 链接\n"
                "🌐 **全网来源/相关动态**：列出查到的真实链接、视频、帖子或动态（带平台前缀）\n"
                "3. 【禁令】：绝对禁止向用户输出类似'建议你在X、Pixiv、B站搜索...'等空泛废话！系统已经为你完成了全网实时检索，你必须直接总结和呈现上方给出的实际命中链接与作者！若所有候选列表均为空，才简短说明推测角色与作品。\n"
                "字数保持在 200 字以内，排版清晰美观，不需要寒暄。"
            )

            resp = await prov.text_chat(prompt=prompt)
            if resp and hasattr(resp, "completion_text") and resp.completion_text:
                return resp.completion_text.strip()

        except Exception as e:
            logger.debug(f"MultimodalAnalyzer: 智能总结调用跳过 ({e})")

        return None
