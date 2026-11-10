"""TraceMoe 动漫画面帧识别引擎。
专用于动漫截图，可精确找出所属番剧、季数、第几集以及精确到秒的时间戳。
"""

from typing import List, Optional

import httpx
try:
    from astrbot.api import logger
except ImportError:
    import logging
    logger = logging.getLogger("astrbot_plugin_picfinder.tracemoe")

from .base import EngineSearchResult, SearchResultItem


class TraceMoeEngine:
    """TraceMoe 动漫截图识别引擎"""

    def __init__(self, proxy: str = ""):
        self.proxy = proxy.strip() if proxy else ""

    async def search(self, image_url: str, local_path: Optional[str] = None, max_results: int = 1) -> EngineSearchResult:
        """执行 TraceMoe 动漫识别"""
        items: List[SearchResultItem] = []
        error_msg: Optional[str] = None
        best_sim = 0.0

        try:
            mounts = {}
            if self.proxy:
                mounts = {"http://": httpx.AsyncHTTPTransport(proxy=self.proxy), "https://": httpx.AsyncHTTPTransport(proxy=self.proxy)}

            async with httpx.AsyncClient(mounts=mounts, timeout=12.0) as client:
                params = {"url": image_url}
                resp = await client.get("https://api.trace.moe/search", params=params)
                if resp.status_code == 200:
                    data = resp.json()
                    results = data.get("result") or []
                    for res in results[:max_results]:
                        sim = float(res.get("similarity", 0.0)) * 100
                        if sim < 80.0:  # 动漫截图识别阈值通常要求 80% 以上
                            continue

                        if sim > best_sim:
                            best_sim = sim

                        filename = res.get("filename") or ""
                        episode = res.get("episode")
                        from_sec = int(res.get("from", 0))
                        mins = from_sec // 60
                        secs = from_sec % 60
                        time_str = f"{mins:02d}:{secs:02d}"

                        title = f"{filename} (第 {episode} 集 {time_str})" if episode else f"{filename} ({time_str})"
                        thumb = res.get("image") or ""
                        video_url = res.get("video") or ""

                        items.append(
                            SearchResultItem(
                                title=title,
                                url=video_url,
                                platform="动画番剧",
                                author="",
                                thumbnail=thumb,
                                similarity=sim,
                                source_engine="TraceMoe",
                                is_exact_match=(sim >= 88.0),
                                extra_info={"episode": episode, "time": time_str, "badge": "🎬 [动画番剧]"},
                            )
                        )
        except Exception as e:
            logger.debug(f"TraceMoe 截图识别跳过: {e}")
            error_msg = str(e)

        return EngineSearchResult(
            engine_name="TraceMoe",
            items=items,
            error=error_msg if not items else None,
            best_similarity=best_sim,
        )
