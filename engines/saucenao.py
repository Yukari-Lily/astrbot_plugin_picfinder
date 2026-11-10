"""SauceNAO 二次元精准搜图引擎封装。
专注于 Pixiv、Danbooru、Twitter 插画、Manga 等的高精度识别。
"""

import traceback
from typing import Any, Dict, List, Optional

import httpx
try:
    from astrbot.api import logger
except ImportError:
    import logging
    logger = logging.getLogger("astrbot_plugin_picfinder.saucenao")

from .base import EngineSearchResult, SearchResultItem
try:
    from ..normalizer.platform_matcher import normalize_platform_url
except (ImportError, ValueError):
    from normalizer.platform_matcher import normalize_platform_url


class SauceNAOEngine:
    """SauceNAO 搜图引擎"""

    def __init__(self, api_key: str = "", threshold: float = 70.0, proxy: str = ""):
        self.api_key = api_key.strip() if api_key else ""
        self.threshold = float(threshold)
        self.proxy = proxy.strip() if proxy else ""

    async def search(self, image_url: str, local_path: Optional[str] = None, max_results: int = 3) -> EngineSearchResult:
        """执行 SauceNAO 检索"""
        items: List[SearchResultItem] = []
        error_msg: Optional[str] = None
        best_sim = 0.0

        # 首选直接使用 SauceNAO 官方 JSON API，稳定可控
        try:
            items, best_sim = await self._search_direct_api(image_url, max_results)
        except Exception as e:
            logger.warning(f"SauceNAO API 直接请求失败 ({e})，尝试 PicImageSearch 备用...")
            try:
                items, best_sim = await self._search_via_picimagesearch(image_url, local_path, max_results)
            except Exception as e2:
                error_msg = f"SauceNAO 检索失败: {e2}"
                logger.error(f"SauceNAO: 备用检索亦异常: {e2}")

        if not items and not error_msg:
            error_msg = "SauceNAO 未找到匹配结果"

        return EngineSearchResult(
            engine_name="SauceNAO",
            items=items,
            error=error_msg if not items else None,
            best_similarity=best_sim,
        )

    async def _search_direct_api(self, image_url: str, max_results: int) -> tuple[List[SearchResultItem], float]:
        """直接调用 SauceNAO search.php 接口"""
        params = {
            "output_type": 2,
            "numres": max_results,
            "db": 999,
            "url": image_url,
        }
        if self.api_key:
            params["api_key"] = self.api_key

        mounts = {}
        if self.proxy:
            mounts = {"http://": httpx.AsyncHTTPTransport(proxy=self.proxy), "https://": httpx.AsyncHTTPTransport(proxy=self.proxy)}

        async with httpx.AsyncClient(mounts=mounts, timeout=15.0) as client:
            resp = await client.get("https://saucenao.com/search.php", params=params)
            if resp.status_code != 200:
                raise RuntimeError(f"HTTP {resp.status_code}")

            data = resp.json()
            results = data.get("results") or []
            items: List[SearchResultItem] = []
            best_sim = 0.0

            for res in results:
                header = res.get("header") or {}
                res_data = res.get("data") or {}

                sim_str = str(header.get("similarity", "0"))
                try:
                    sim = float(sim_str)
                except ValueError:
                    sim = 0.0

                if sim > best_sim:
                    best_sim = sim

                ext_urls = res_data.get("ext_urls") or []
                target_url = ext_urls[0] if ext_urls else ""

                title, author = self._extract_sauce_title_author(header, res_data)
                thumb = header.get("thumbnail") or ""

                norm = normalize_platform_url(target_url, title=title)
                if not author and norm.author:
                    author = norm.author

                item = SearchResultItem(
                    title=title,
                    url=norm.canonical_url or target_url,
                    platform=norm.platform_name,
                    author=author,
                    thumbnail=thumb,
                    similarity=sim,
                    source_engine="SauceNAO",
                    is_exact_match=(sim >= self.threshold),
                    extra_info={"badge": norm.badge, "index_id": header.get("index_id")},
                )
                items.append(item)

            return items, best_sim

    def _extract_sauce_title_author(self, header: Dict[str, Any], data: Dict[str, Any]) -> tuple[str, str]:
        """提取 SauceNAO 细分作品标题与作者"""
        title = data.get("title") or ""
        author = data.get("member_name") or data.get("author_name") or data.get("creator") or ""

        # Twitter 特别字段
        if not author and "twitter_user_handle" in data:
            author = data["twitter_user_handle"]

        # Skeb
        if not author and "creator_name" in data:
            author = data["creator_name"]

        # Manga / Book
        if not title and "source" in data:
            title = data["source"]
            if "part" in data:
                title = f"{title} - {data['part']}"

        return str(title), str(author)

    async def _search_via_picimagesearch(
        self, image_url: str, local_path: Optional[str], max_results: int
    ) -> tuple[List[SearchResultItem], float]:
        """通过 PicImageSearch 调用 SauceNAO 备用"""
        from PicImageSearch import Network, SauceNAO

        proxies = self.proxy if self.proxy else None
        items: List[SearchResultItem] = []
        best_sim = 0.0

        async with Network(proxies=proxies) as client:
            saucenao = SauceNAO(client=client, api_key=self.api_key if self.api_key else None, numres=max_results)
            if local_path:
                resp = await saucenao.search(file=local_path)
            else:
                resp = await saucenao.search(url=image_url)

            if not resp or not hasattr(resp, "raw") or not resp.raw:
                return [], 0.0

            for item in resp.raw:
                sim = float(getattr(item, "similarity", 0.0) or 0.0)
                if sim > best_sim:
                    best_sim = sim

                target_url = getattr(item, "url", "") or ""
                title = getattr(item, "title", "") or ""
                author = getattr(item, "author", "") or ""
                thumb = getattr(item, "thumbnail", "") or ""

                norm = normalize_platform_url(target_url, title=title)
                if not author and norm.author:
                    author = norm.author

                items.append(
                    SearchResultItem(
                        title=title,
                        url=norm.canonical_url or target_url,
                        platform=norm.platform_name,
                        author=author,
                        thumbnail=thumb,
                        similarity=sim,
                        source_engine="SauceNAO",
                        is_exact_match=(sim >= self.threshold),
                        extra_info={"badge": norm.badge},
                    )
                )

        return items, best_sim
