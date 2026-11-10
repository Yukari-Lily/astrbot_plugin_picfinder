"""从图片与搜图结果中深度提取平台作者/画师，并检索该作者的其他作品、相册与主页。
"""

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import httpx

try:
    from astrbot.api import logger
except ImportError:
    import logging
    logger = logging.getLogger("astrbot_plugin_picfinder.author_extractor")

try:
    from ..engines.base import EngineSearchResult, SearchResultItem
except (ImportError, ValueError):
    from engines.base import EngineSearchResult, SearchResultItem
from .platform_matcher import NormalizedPlatformInfo, normalize_platform_url


@dataclass
class ArtworkSample:
    """画师的其他作品样本"""
    title: str = ""
    page_url: str = ""
    image_url: str = ""
    character_tags: List[str] = field(default_factory=list)
    platform: str = ""


@dataclass
class AuthorProfile:
    """平台作者/画师画像"""
    name: str = ""
    primary_platform: str = "未知"  # X (Twitter), Pixiv, 哔哩哔哩, YouTube 等
    handle: str = ""  # 推特 handle 等
    user_id: str = ""  # Pixiv UID 或 B站 UID
    profile_url: str = ""  # 作者个人主页
    gallery_url: str = ""  # 媒体相册/作品集直达链接 (如 x.com/user/media 或 pixiv.net/users/uid/artworks)
    other_platforms: Dict[str, str] = field(default_factory=dict)  # 其他平台链接 (如 {"Pixiv": "...", "X": "..."})
    danbooru_tag: str = ""  # Danbooru 画师 tag，用于查询更多图
    sample_artworks: List[ArtworkSample] = field(default_factory=list)  # 该画师的其他作品列表
    representative_works: List[str] = field(default_factory=list)  # 代表作/参与企划推测


class AuthorArtworksManager:
    """画师信息与更多作品抓取器"""

    def __init__(self, proxy: str = ""):
        self.proxy = proxy.strip() if proxy else ""

    def extract_author_profile(
        self,
        engine_results: List[EngineSearchResult],
        ai_analysis: Optional[Any] = None,
    ) -> AuthorProfile:
        """从 Google Lens、SauceNAO、Ascii2D 以及 AI 分析结果中综合提取最核心的作者画像"""
        profile = AuthorProfile()

        # 1. 优先从 SauceNAO 提取高精度 Pixiv / Twitter 画师
        sauce_res = next((er for er in engine_results if er.engine_name == "SauceNAO"), None)
        if sauce_res and sauce_res.items:
            for it in sauce_res.items:
                if it.platform == "Pixiv" and it.author:
                    profile.name = it.author
                    profile.primary_platform = "Pixiv"
                    norm = normalize_platform_url(it.url)
                    if norm.item_id:
                        # 从作品反推画师主页或从 extra_info 获取
                        pass
                    if "pixiv.net/users/" in it.url:
                        profile.profile_url = it.url
                    elif it.url.startswith("https://www.pixiv.net/artworks/"):
                        # 记录 Pixiv 原链
                        profile.other_platforms["Pixiv作品"] = it.url
                    break
                elif it.platform == "X (Twitter)" and it.author:
                    profile.name = it.author
                    profile.handle = it.author
                    profile.primary_platform = "X (Twitter)"
                    profile.profile_url = f"https://x.com/{it.author}"
                    profile.gallery_url = f"https://x.com/{it.author}/media"
                    break
                elif it.platform in ("Danbooru", "Gelbooru") and it.author:
                    profile.danbooru_tag = it.author
                    if not profile.name:
                        profile.name = it.author

        # 2. 从 Google Lens 或 AI 联网检索提取 X (Twitter) 画师或 Bilibili / Pixiv / 微博 / 抖音出处
        for er in engine_results:
            if er.engine_name not in ("Google Lens", "AI 联网检索"):
                continue
            for it in er.items:
                norm = normalize_platform_url(it.url, title=it.title)
                author_cand = it.author or norm.author
                if norm.platform_name == "X (Twitter)" and author_cand:
                    if not profile.handle:
                        profile.handle = author_cand
                    if not profile.name or profile.primary_platform == "未知":
                        profile.name = author_cand
                        profile.primary_platform = "X (Twitter)"
                    profile.other_platforms["X (Twitter)"] = f"https://x.com/{author_cand}"
                    profile.gallery_url = f"https://x.com/{author_cand}/media"
                elif norm.platform_name == "Pixiv" and norm.item_id:
                    if "Pixiv" not in profile.other_platforms:
                        profile.other_platforms["Pixiv"] = norm.canonical_url
                    if not profile.name and author_cand:
                        profile.name = author_cand
                        profile.primary_platform = "Pixiv"
                elif norm.platform_name == "哔哩哔哩" and norm.canonical_url:
                    profile.other_platforms["哔哩哔哩"] = norm.canonical_url
                    if not profile.name and author_cand:
                        profile.name = author_cand
                        profile.primary_platform = "哔哩哔哩"
                elif norm.platform_name in ("微博", "抖音", "米游社", "小红书") and norm.canonical_url:
                    profile.other_platforms[norm.platform_name] = norm.canonical_url
                    if not profile.name and author_cand:
                        profile.name = author_cand
                        profile.primary_platform = norm.platform_name
                elif norm.platform_name == "YouTube" and norm.canonical_url:
                    profile.other_platforms["YouTube"] = norm.canonical_url

        # 3. 如果仍未获得有效作者，使用 AI 画面分析中的推测作者/签名
        if ai_analysis and not profile.name:
            artist_pred = getattr(ai_analysis, "artist_or_author", "")
            if artist_pred and artist_pred != "无":
                profile.name = artist_pred
                profile.primary_platform = "AI 画面识别"

        # 规范化画师标签 (用于图库检索)
        if not profile.danbooru_tag and profile.name:
            tag = profile.name.strip().replace(" ", "_")
            profile.danbooru_tag = tag

        # 构建主页与作品集链接
        if profile.primary_platform == "X (Twitter)" and profile.handle:
            profile.profile_url = f"https://x.com/{profile.handle}"
            profile.gallery_url = f"https://x.com/{profile.handle}/media"
        elif profile.primary_platform == "Pixiv" and profile.user_id:
            profile.profile_url = f"https://www.pixiv.net/users/{profile.user_id}"
            profile.gallery_url = f"https://www.pixiv.net/users/{profile.user_id}/artworks"

        return profile

    async def fetch_author_other_artworks(self, profile: AuthorProfile, limit: int = 5) -> List[ArtworkSample]:
        """通过公开图库 API 获取该画师的其他插画作品列表与直链预览"""
        samples: List[ArtworkSample] = []
        tag = profile.danbooru_tag or profile.handle or profile.name
        if not tag:
            return samples

        clean_tag = tag.lower().replace(" ", "_").strip()
        # 排除太短或无意义的 tag
        if len(clean_tag) < 2 or clean_tag in ("unknown", "various", "original", "none"):
            return samples

        mounts = {}
        if self.proxy:
            mounts = {
                "http://": httpx.AsyncHTTPTransport(proxy=self.proxy),
                "https://": httpx.AsyncHTTPTransport(proxy=self.proxy),
            }

        # 1. 尝试从 Danbooru 获取该作者作品
        try:
            api_url = f"https://danbooru.donmai.us/posts.json?tags={quote(clean_tag)}&limit={limit}"
            headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AstrBot/4.0"}
            async with httpx.AsyncClient(mounts=mounts, timeout=8.0) as client:
                resp = await client.get(api_url, headers=headers)
                if resp.status_code == 200:
                    data = resp.json()
                    if isinstance(data, list):
                        for post in data:
                            if not isinstance(post, dict):
                                continue
                            post_id = post.get("id")
                            img_url = post.get("large_file_url") or post.get("file_url") or post.get("preview_file_url") or ""
                            char_tag = post.get("tag_string_character", "")
                            chars = [c for c in char_tag.split() if c][:3]
                            source_url = post.get("source") or f"https://danbooru.donmai.us/posts/{post_id}"

                            if post_id:
                                samples.append(
                                    ArtworkSample(
                                        title=f"作品 #{post_id}" + (f" ({'、'.join(chars)})" if chars else ""),
                                        page_url=source_url,
                                        image_url=img_url,
                                        character_tags=chars,
                                        platform="Danbooru",
                                    )
                                )
        except Exception as e:
            logger.debug(f"fetch_author_other_artworks (Danbooru) 跳过: {e}")

        # 2. 如果 Danbooru 未搜到且有推特 handle，尝试 Gelbooru
        if not samples and profile.handle:
            try:
                g_url = f"https://gelbooru.com/index.php?page=dapi&s=post&q=index&json=1&tags={quote(profile.handle)}&limit={limit}"
                async with httpx.AsyncClient(mounts=mounts, timeout=8.0) as client:
                    resp = await client.get(g_url)
                    if resp.status_code == 200:
                        data = resp.json()
                        posts = data.get("post") or []
                        for p in posts:
                            pid = p.get("id")
                            file_url = p.get("file_url") or ""
                            samples.append(
                                ArtworkSample(
                                    title=f"作品 #{pid}",
                                    page_url=f"https://gelbooru.com/index.php?page=post&s=view&id={pid}",
                                    image_url=file_url,
                                    platform="Gelbooru",
                                )
                            )
            except Exception as e:
                logger.debug(f"fetch_author_other_artworks (Gelbooru) 跳过: {e}")

        profile.sample_artworks = samples
        return samples

    def format_author_summary(self, profile: AuthorProfile) -> str:
        """生成面向聊天模型与用户的完整画师画像与作品报告"""
        lines = []
        name_display = profile.name or profile.handle or "未知作者"
        lines.append(f"🎨 【平台作者/画师画像】：{name_display}")
        lines.append(f"• 活跃主平台: {profile.primary_platform}")

        if profile.handle:
            lines.append(f"• X (Twitter) 账号: @{profile.handle}")
            lines.append(f"  - 主页: https://x.com/{profile.handle}")
            lines.append(f"  - 媒体插画相册: https://x.com/{profile.handle}/media")

        if profile.gallery_url and "x.com" not in profile.gallery_url:
            lines.append(f"• 作品相册直达: {profile.gallery_url}")

        if profile.other_platforms:
            lines.append("• 全网其他平台主页/关联：")
            for plat, u in profile.other_platforms.items():
                lines.append(f"  - [{plat}]: {u}")

        if profile.sample_artworks:
            lines.append(f"\n🖼️ 【该画师的其他代表作/插画列表 ({len(profile.sample_artworks)} 张)】：")
            for idx, art in enumerate(profile.sample_artworks, 1):
                char_str = f" [角色: {'、'.join(art.character_tags)}]" if art.character_tags else ""
                lines.append(f"{idx}. {art.title}{char_str}")
                lines.append(f"   🔗 原作出处: {art.page_url}")
                if art.image_url:
                    lines.append(f"   🖼️ 图片直链: {art.image_url}")
        else:
            lines.append("\n💡 提示：可通过上述作者的 X (Twitter) 媒体相册或 Pixiv 主页直接浏览其海量作品。")

        return "\n".join(lines)
