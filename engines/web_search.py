"""AI 自动全网联网检索引擎 (WebSearchEngine)。
当以图搜图未找到完全匹配原图时，提取画面特征、作品、角色、水印与作者，
全网检索（DuckDuckGo / Bilibili / X / 微博 / 米游社等）获取真实出处链接与动态。
"""

import asyncio
import re
from typing import Any, Dict, List, Optional, Set
from urllib.parse import quote, unquote

import httpx

try:
    from curl_cffi.requests import AsyncSession
    HAVE_CURL_CFFI = True
except ImportError:
    HAVE_CURL_CFFI = False

try:
    from astrbot.api import logger
except ImportError:
    import logging
    logger = logging.getLogger("astrbot_plugin_picfinder.web_search")

try:
    from .base import EngineSearchResult, SearchResultItem
    from ..normalizer.platform_matcher import extract_author_hint, normalize_platform_url
except (ImportError, ValueError):
    from engines.base import EngineSearchResult, SearchResultItem
    from normalizer.platform_matcher import extract_author_hint, normalize_platform_url


# 繁简体常见转换字典 (水印常见字)
_CHAR_TRAD_TO_SIMP = {
    "藍": "蓝",
    "櫻": "樱",
    "醬": "酱",
    "貓": "猫",
    "愛": "爱",
    "華": "华",
    "夢": "梦",
    "葉": "叶",
    "優": "优",
    "鈴": "铃",
    "風": "风",
    "靈": "灵",
    "龍": "龙",
    "麗": "丽",
    "魚": "鱼",
    "鳥": "鸟",
    "寶": "宝",
    "貝": "贝",
}


def clean_watermark_text(text: str) -> str:
    """清理水印、OCR 文本中的杂质表情与无用标点"""
    if not text:
        return ""
    # 移除表情符号与无用特殊符号
    cleaned = re.sub(r"[♡❤♥★☆✨~_@#￥%&*（）()\[\]【】\.\!\?！？，,。/\\\|\-]+", " ", text)
    # 移除末尾的 "及"、"等"、"..."
    cleaned = re.sub(r"(?:及|等|\.\.\.|…)+\s*$", "", cleaned.strip())
    return cleaned.strip()


def to_simplified_variant(text: str) -> str:
    """生成繁转简候选词"""
    res = []
    has_diff = False
    for ch in text:
        simp = _CHAR_TRAD_TO_SIMP.get(ch, ch)
        if simp != ch:
            has_diff = True
        res.append(simp)
    return "".join(res) if has_diff else ""


class WebSearchEngine:
    """AI 辅助互联网逆向检索调度器"""

    def __init__(self, proxy: str = ""):
        self.proxy = proxy.strip() if proxy else ""

    def generate_queries_from_analysis(self, analysis: Any) -> List[str]:
        """根据 AI 视觉画面分析（角色、作品、水印OCR、推测作者）提炼多组高质量搜索词"""
        if not analysis:
            return []

        subject_type = getattr(analysis, "subject_type", "") or ""
        characters = getattr(analysis, "characters", []) or []
        origin_work = getattr(analysis, "origin_work", "") or ""
        ocr_text = getattr(analysis, "ocr_text", "") or ""
        artist_or_author = getattr(analysis, "artist_or_author", "") or ""
        raw_keywords = getattr(analysis, "search_keywords", []) or []

        queries: List[str] = []

        clean_wm = clean_watermark_text(ocr_text)
        clean_author = clean_watermark_text(artist_or_author)
        clean_work = re.sub(r"[《》]+", "", origin_work).strip()

        is_cos = "cos" in subject_type.lower() or "真人" in subject_type
        type_tag = "cos" if is_cos else ""

        # 繁简转换水印
        wm_simp = to_simplified_variant(clean_wm) if clean_wm else ""

        # 1. 组合：角色 + 水印/作者 + 形式 (cos / 动态)
        for char in characters[:2]:
            if clean_wm:
                queries.append(f"{char} {type_tag} {clean_wm}".strip())
                if wm_simp:
                    queries.append(f"{char} {type_tag} {wm_simp}".strip())
                if clean_work:
                    queries.append(f"{clean_work} {char} {clean_wm}".strip())
            if clean_author and clean_author != clean_wm:
                queries.append(f"{char} {type_tag} {clean_author}".strip())

        # 2. 如果没有角色，但有水印与作品
        if not characters and clean_wm:
            if clean_work:
                queries.append(f"{clean_work} {type_tag} {clean_wm}".strip())
            else:
                queries.append(f"{clean_wm} {type_tag}".strip())

        # 3. 将大模型给出的 search_keywords 也加入并清洗
        for kw in raw_keywords:
            c = clean_watermark_text(kw)
            if c:
                queries.append(c)
                simp = to_simplified_variant(c)
                if simp:
                    queries.append(simp)

        # 4. 兜底备用：作品 + 角色 + 形式
        if not queries and characters:
            for char in characters[:2]:
                queries.append(f"{clean_work} {char} {type_tag}".strip())

        # 去重且保持顺序
        seen: Set[str] = set()
        final_queries: List[str] = []
        for q in queries:
            normalized = " ".join(q.split())
            if normalized and len(normalized) >= 2 and normalized not in seen:
                seen.add(normalized)
                final_queries.append(normalized)

        return final_queries

    async def search_ddg(self, query: str, limit: int = 5) -> List[SearchResultItem]:
        """通过 DuckDuckGo HTML 引擎抓取检索结果并解析"""
        url = f"https://html.duckduckgo.com/html/?q={quote(query)}"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }

        html = ""
        # 1. 优先使用 curl_cffi (防封禁能力更强)
        if HAVE_CURL_CFFI:
            try:
                proxies = {"http": self.proxy, "https": self.proxy} if self.proxy else None
                async with AsyncSession(proxies=proxies) as session:
                    resp = await session.get(url, headers=headers, impersonate="chrome120", timeout=8)
                    if resp.status_code == 200:
                        html = resp.text
            except Exception as e:
                logger.debug(f"WebSearchEngine: curl_cffi 请求异常 ({e})，尝试降级 httpx")

        # 2. 降级使用 httpx
        if not html:
            try:
                mounts = {}
                if self.proxy:
                    mounts = {
                        "http://": httpx.AsyncHTTPTransport(proxy=self.proxy),
                        "https://": httpx.AsyncHTTPTransport(proxy=self.proxy),
                    }
                async with httpx.AsyncClient(mounts=mounts, timeout=8.0, headers=headers) as client:
                    resp = await client.get(url)
                    if resp.status_code == 200:
                        html = resp.text
            except Exception as e:
                logger.debug(f"WebSearchEngine: httpx 抓取失败 ({e})")

        if not html:
            return []

        # 解析 DuckDuckGo HTML 结果
        pattern = re.compile(
            r'<h2 class="result__title">\s*<a[^>]+class="result__a"[^>]+href="//duckduckgo\.com/l/\?uddg=([^"&]+)[^>]*>(.*?)</a>.*?'
            r'<a[^>]+class="result__snippet"[^>]*>(.*?)</a>',
            re.DOTALL,
        )

        items: List[SearchResultItem] = []
        for m in pattern.finditer(html):
            encoded_url, raw_title, raw_snippet = m.groups()
            real_url = unquote(encoded_url)

            clean_title = re.sub(r"<[^>]+>", "", raw_title).strip()
            clean_snippet = re.sub(r"<[^>]+>", "", raw_snippet).strip()

            # 过滤垃圾爬虫站或无意义链接
            if any(junk in real_url.lower() for junk in ("duckduckgo.com", "baidu.com/link")):
                continue

            norm = normalize_platform_url(real_url, title=clean_title)
            author = norm.author or extract_author_hint(clean_title, clean_snippet)

            item = SearchResultItem(
                title=clean_title,
                url=norm.canonical_url or real_url,
                platform=norm.platform_name,
                author=author,
                source_engine="AI 联网检索",
                similarity=85.0 if norm.priority_weight >= 80 else 70.0,
                extra_info={
                    "badge": norm.badge,
                    "snippet": clean_snippet,
                    "priority_weight": norm.priority_weight,
                },
            )
            items.append(item)
            if len(items) >= limit:
                break

        return items

    async def search_by_queries(self, queries: List[str], max_items: int = 4) -> EngineSearchResult:
        """对候选搜索词并发或顺序检索，合并与去重全网出处"""
        engine_res = EngineSearchResult(engine_name="AI 联网检索")
        if not queries:
            return engine_res

        all_items: List[SearchResultItem] = []
        seen_urls: Set[str] = set()

        # 最多并发查询前 3 组关键词
        target_queries = queries[:3]
        tasks = [self.search_ddg(q, limit=max_items) for q in target_queries]
        results_list = await asyncio.gather(*tasks, return_exceptions=True)

        for res in results_list:
            if isinstance(res, list):
                for item in res:
                    if not item.url or item.url in seen_urls:
                        continue
                    seen_urls.add(item.url)
                    all_items.append(item)

        # 按照平台权重排序（X/B站/米游社优先）
        all_items.sort(key=lambda x: x.extra_info.get("priority_weight", 0), reverse=True)

        engine_res.items = all_items[:max_items]
        if engine_res.items:
            engine_res.best_similarity = max(it.similarity for it in engine_res.items)

        return engine_res
