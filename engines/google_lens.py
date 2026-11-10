"""Google Lens (谷歌识图) 引擎封装。
支持全网索引，优先提取视觉匹配 (Visual Matches) 与 X / Bilibili / YouTube / Pixiv 平台来源。
"""

import json
import re
from typing import List, Optional
from urllib.parse import quote

try:
    from astrbot.api import logger
except ImportError:
    import logging
    logger = logging.getLogger("astrbot_plugin_picfinder.google_lens")

from .base import EngineSearchResult, SearchResultItem
try:
    from ..normalizer.platform_matcher import normalize_platform_url
except (ImportError, ValueError):
    from normalizer.platform_matcher import normalize_platform_url


class GoogleLensEngine:
    """Google Lens 搜图引擎"""

    def __init__(self, proxy: str = ""):
        self.proxy = proxy.strip() if proxy else ""

    async def search(self, image_url: str, local_path: Optional[str] = None, max_results: int = 5) -> EngineSearchResult:
        """执行 Google Lens 识图检索"""
        items: List[SearchResultItem] = []
        error_msg: Optional[str] = None

        # 尝试使用 PicImageSearch
        try:
            items = await self._search_via_picimagesearch(image_url, local_path, max_results)
        except Exception as e:
            logger.warning(f"GoogleLens: PicImageSearch 检索异常 ({e})，尝试直接解析...")
            try:
                items = await self._search_direct_lens(image_url, max_results)
            except Exception as e2:
                error_msg = f"Google Lens 检索失败: {e2}"
                logger.error(f"GoogleLens: 直接解析亦失败: {e2}")

        # 如果两者都空且没有记录错误
        if not items and not error_msg:
            error_msg = "Google Lens 未能获取到匹配结果"

        return EngineSearchResult(
            engine_name="Google Lens",
            items=items,
            error=error_msg if not items else None,
            best_similarity=95.0 if items else 0.0,
        )

    async def _search_via_picimagesearch(
        self, image_url: str, local_path: Optional[str], max_results: int
    ) -> List[SearchResultItem]:
        """通过 PicImageSearch 库调用 GoogleLens"""
        from PicImageSearch import GoogleLens, Network

        proxies = self.proxy if self.proxy else None
        items: List[SearchResultItem] = []

        async with Network(proxies=proxies) as client:
            lens = GoogleLens(client=client)
            if local_path:
                resp = await lens.search(file=local_path)
            else:
                resp = await lens.search(url=image_url)

            if not resp or not hasattr(resp, "raw") or not resp.raw:
                return []

            # 遍历解析返回的原始条目
            count = 0
            for item in resp.raw:
                if count >= max_results:
                    break
                title = getattr(item, "title", "") or ""
                target_url = getattr(item, "url", "") or ""
                thumbnail = getattr(item, "thumbnail", "") or ""
                source_domain = getattr(item, "source", "") or ""

                if not target_url:
                    continue

                # 平台归类
                norm = normalize_platform_url(target_url, title=title)

                search_item = SearchResultItem(
                    title=title,
                    url=norm.canonical_url or target_url,
                    platform=norm.platform_name,
                    author=norm.author,
                    thumbnail=thumbnail,
                    similarity=90.0 if count == 0 else 80.0,
                    source_engine="Google Lens",
                    is_exact_match=(count == 0),  # 首位为 Google Lens 最佳视觉匹配
                    extra_info={"badge": norm.badge, "source_domain": source_domain},
                )
                items.append(search_item)
                count += 1

        return items

    async def _search_direct_lens(self, image_url: str, max_results: int) -> List[SearchResultItem]:
        """直接请求 Google Lens uploadbyurl 备用方案 (基于 curl_cffi 模拟浏览器)"""
        from curl_cffi.requests import AsyncSession

        lens_upload_url = f"https://lens.google.com/uploadbyurl?url={quote(image_url)}&hl=zh-CN"
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7,ja;q=0.6",
        }
        proxies = {"all": self.proxy} if self.proxy else None

        async with AsyncSession(impersonate="chrome120", proxies=proxies, timeout=25) as session:
            resp = await session.get(lens_upload_url, headers=headers, allow_redirects=True)
            if resp.status_code != 200:
                raise RuntimeError(f"HTTP {resp.status_code}")

            text = resp.text
            items: List[SearchResultItem] = []

            # 提取 AF_initDataCallback 包含的视觉匹配 JSON 数据
            # Google Lens 在页面渲染中通过 AF_initDataCallback 注入数据
            pattern = re.compile(r"AF_initDataCallback\((\{.*?key:\s*'ds:1'.*?\})\);", re.DOTALL)
            m = pattern.search(text)
            if not m:
                # 尝试更宽泛的 key 匹配
                pattern2 = re.compile(r"AF_initDataCallback\((\{.*?data:.*?function\(\)\{return\s+(\[.*?\])\}\s*\}\);", re.DOTALL)
                m2 = pattern2.search(text)
                if not m2:
                    return []
                raw_json = m2.group(2)
            else:
                data_block = m.group(1)
                data_match = re.search(r"data:\s*(function\(\)\{return\s+)?(\[.*?\])\s*\}?\s*\}?$", data_block, re.DOTALL)
                raw_json = data_match.group(2) if data_match else ""

            if raw_json:
                try:
                    data = json.loads(raw_json)
                    # 递归遍历提取链接与标题
                    extracted = self._extract_lens_items_from_json(data, max_results)
                    for target_url, title, thumb in extracted:
                        norm = normalize_platform_url(target_url, title=title)
                        items.append(
                            SearchResultItem(
                                title=title,
                                url=norm.canonical_url or target_url,
                                platform=norm.platform_name,
                                author=norm.author,
                                thumbnail=thumb,
                                similarity=90.0 if not items else 80.0,
                                source_engine="Google Lens",
                                is_exact_match=(len(items) == 0),
                                extra_info={"badge": norm.badge},
                            )
                        )
                except Exception as e:
                    logger.debug(f"GoogleLens: JSON 解析跳过: {e}")

            return items

    def _extract_lens_items_from_json(self, obj, max_results: int) -> List[tuple]:
        """从 Google Lens 嵌套数据中递归寻找条目 (url, title, thumbnail)"""
        results = []
        visited_urls = set()

        def _traverse(node):
            if len(results) >= max_results:
                return
            if isinstance(node, dict):
                for v in node.values():
                    _traverse(v)
            elif isinstance(node, list):
                # 检查典型模式: [..., url, title, ...]
                url = None
                title = ""
                thumb = ""
                for elem in node:
                    if isinstance(elem, str):
                        if (elem.startswith("http://") or elem.startswith("https://")) and "google.com" not in elem:
                            # 区分缩略图与目标网页
                            if any(ext in elem.lower() for ext in (".jpg", ".jpeg", ".png", ".webp", ".gif", "encrypted-tbn", "/thumb")):
                                if not thumb:
                                    thumb = elem
                            else:
                                if not url:
                                    url = elem
                        elif len(elem) > 3 and not elem.startswith("http") and not title:
                            title = elem
                if not url and thumb:
                    url = thumb
                if url and url not in visited_urls:
                    visited_urls.add(url)
                    results.append((url, title, thumb))
                for item in node:
                    _traverse(item)

        _traverse(obj)
        return results
