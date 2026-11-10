"""互联网多平台来源识别器与 URL 规范化工具。
支持识别 X (Twitter)、哔哩哔哩 (Bilibili)、YouTube、Pixiv、ArtStation 等来源。
"""

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse


@dataclass
class NormalizedPlatformInfo:
    """平台识别结果元数据"""
    platform_name: str
    canonical_url: str
    author: str = ""
    item_id: str = ""
    badge: str = ""  # 平台图标/徽标文字，如 🐦 [X / Twitter]
    priority_weight: int = 50  # 来源权重（原发平台更高）

    def to_dict(self) -> Dict[str, Any]:
        """序列化为字典方便 LLM 工具调用输出"""
        return {
            "platform": self.platform_name,
            "canonical_url": self.canonical_url,
            "author": self.author,
            "item_id": self.item_id,
            "badge": self.badge,
            "priority_weight": self.priority_weight,
        }


# 预编译常用平台的正则规则
_X_REGEX = re.compile(
    r"https?://(?:www\.)?(?:twitter\.com|x\.com)/([a-zA-Z0-9_]{1,30})/status/(\d+)",
    re.IGNORECASE,
)
_X_PROFILE_REGEX = re.compile(
    r"https?://(?:www\.)?(?:twitter\.com|x\.com)/([a-zA-Z0-9_]{1,30})(?:/)?$",
    re.IGNORECASE,
)

_BILI_VIDEO_REGEX = re.compile(
    r"https?://(?:www\.)?bilibili\.com/video/(BV[a-zA-Z0-9]{10}|av\d+)",
    re.IGNORECASE,
)
_BILI_OPUS_REGEX = re.compile(
    r"https?://(?:www\.)?bilibili\.com/opus/(\d+)",
    re.IGNORECASE,
)
_BILI_DYNAMIC_REGEX = re.compile(
    r"https?://t\.bilibili\.com/(\d+)",
    re.IGNORECASE,
)
_BILI_READ_REGEX = re.compile(
    r"https?://(?:www\.)?bilibili\.com/read/cv(\d+)",
    re.IGNORECASE,
)

_DOUYIN_NOTE_REGEX = re.compile(
    r"https?://(?:www\.)?douyin\.com/(?:note|video)/(\d+)",
    re.IGNORECASE,
)

_MIYOUSHE_ARTICLE_REGEX = re.compile(
    r"https?://(?:www\.)?miyoushe\.com/[a-zA-Z0-9_-]+/article/(\d+)",
    re.IGNORECASE,
)

_XHS_EXPLORE_REGEX = re.compile(
    r"https?://(?:www\.)?xiaohongshu\.com/explore/([a-zA-Z0-9]+)",
    re.IGNORECASE,
)

_YOUTUBE_REGEX = re.compile(
    r"https?://(?:www\.)?(?:youtube\.com/(?:watch\?v=|shorts/|embed/)|youtu\.be/)([a-zA-Z0-9_-]{11})",
    re.IGNORECASE,
)

_PIXIV_ARTWORK_REGEX = re.compile(
    r"https?://(?:www\.)?pixiv\.net/(?:[a-zA-Z-_]+/)?artworks/(\d+)",
    re.IGNORECASE,
)
_PIXIV_USER_REGEX = re.compile(
    r"https?://(?:www\.)?pixiv\.net/(?:[a-zA-Z-_]+/)?users/(\d+)",
    re.IGNORECASE,
)

_ARTSTATION_REGEX = re.compile(
    r"https?://(?:www\.)?artstation\.com/artwork/([a-zA-Z0-9_-]+)",
    re.IGNORECASE,
)

_SKEB_REGEX = re.compile(
    r"https?://skeb\.jp/@([a-zA-Z0-9_-]+)/works/(\d+)",
    re.IGNORECASE,
)

_NICO_SEIGA_REGEX = re.compile(
    r"https?://seiga\.nicovideo\.jp/seiga/(im\d+)",
    re.IGNORECASE,
)
_NICO_VIDEO_REGEX = re.compile(
    r"https?://(?:www\.)?nicovideo\.jp/watch/(sm\d+|so\d+)",
    re.IGNORECASE,
)

_DANBOORU_REGEX = re.compile(
    r"https?://danbooru\.donmai\.us/posts/(\d+)",
    re.IGNORECASE,
)
_GELBOORU_REGEX = re.compile(
    r"https?://(?:www\.)?gelbooru\.com/index\.php\?page=post&s=view&id=(\d+)",
    re.IGNORECASE,
)


def normalize_platform_url(url: str, title: str = "") -> NormalizedPlatformInfo:
    """根据给定的 URL 深度解析所属平台、出处、作者及标准化链接。"""
    if not url or not isinstance(url, str):
        return NormalizedPlatformInfo(
            platform_name="网页",
            canonical_url="",
            badge="🌐 [网页]",
            priority_weight=10,
        )

    clean_url = url.strip()

    # 1. X (Twitter) 推文与作者
    m = _X_REGEX.search(clean_url)
    if m:
        author, tweet_id = m.group(1), m.group(2)
        # 排除官方与特定系统账号误判
        if author.lower() not in ("i", "intent", "search", "explore", "home"):
            return NormalizedPlatformInfo(
                platform_name="X (Twitter)",
                canonical_url=f"https://x.com/{author}/status/{tweet_id}",
                author=author,
                item_id=tweet_id,
                badge="🐦 [X / Twitter]",
                priority_weight=100,  # 顶级原发平台
            )

    m = _X_PROFILE_REGEX.search(clean_url)
    if m and m.group(1).lower() not in ("i", "intent", "search", "explore", "home"):
        author = m.group(1)
        return NormalizedPlatformInfo(
            platform_name="X (Twitter)",
            canonical_url=f"https://x.com/{author}",
            author=author,
            badge="🐦 [X / Twitter 主页]",
            priority_weight=80,
        )

    # 2. 哔哩哔哩 (Bilibili)
    m = _BILI_VIDEO_REGEX.search(clean_url)
    if m:
        bvid = m.group(1)
        return NormalizedPlatformInfo(
            platform_name="哔哩哔哩",
            canonical_url=f"https://www.bilibili.com/video/{bvid}",
            item_id=bvid,
            badge="📺 [哔哩哔哩 视频]",
            priority_weight=95,
        )

    m = _BILI_OPUS_REGEX.search(clean_url) or _BILI_DYNAMIC_REGEX.search(clean_url)
    if m:
        opus_id = m.group(1)
        return NormalizedPlatformInfo(
            platform_name="哔哩哔哩",
            canonical_url=f"https://www.bilibili.com/opus/{opus_id}",
            item_id=opus_id,
            badge="📺 [哔哩哔哩 动态/专栏]",
            priority_weight=90,
        )

    m = _BILI_READ_REGEX.search(clean_url)
    if m:
        cv_id = m.group(1)
        return NormalizedPlatformInfo(
            platform_name="哔哩哔哩",
            canonical_url=f"https://www.bilibili.com/read/cv{cv_id}",
            item_id=f"cv{cv_id}",
            badge="📺 [哔哩哔哩 专栏]",
            priority_weight=85,
        )

    # 3. 抖音
    m = _DOUYIN_NOTE_REGEX.search(clean_url)
    if m:
        note_id = m.group(1)
        return NormalizedPlatformInfo(
            platform_name="抖音",
            canonical_url=f"https://www.douyin.com/note/{note_id}",
            item_id=note_id,
            badge="🎵 [抖音 作品]",
            priority_weight=90,
        )

    # 4. 米游社
    m = _MIYOUSHE_ARTICLE_REGEX.search(clean_url)
    if m:
        article_id = m.group(1)
        return NormalizedPlatformInfo(
            platform_name="米游社",
            canonical_url=clean_url,
            item_id=article_id,
            badge="🎮 [米游社 动态]",
            priority_weight=90,
        )

    # 5. 小红书
    m = _XHS_EXPLORE_REGEX.search(clean_url)
    if m:
        explore_id = m.group(1)
        return NormalizedPlatformInfo(
            platform_name="小红书",
            canonical_url=f"https://www.xiaohongshu.com/explore/{explore_id}",
            item_id=explore_id,
            badge="📕 [小红书 笔记]",
            priority_weight=90,
        )

    # 3. YouTube
    m = _YOUTUBE_REGEX.search(clean_url)
    if m:
        video_id = m.group(1)
        return NormalizedPlatformInfo(
            platform_name="YouTube",
            canonical_url=f"https://www.youtube.com/watch?v={video_id}",
            item_id=video_id,
            badge="▶️ [YouTube 视频]",
            priority_weight=95,
        )

    # 4. Pixiv
    m = _PIXIV_ARTWORK_REGEX.search(clean_url)
    if m:
        illust_id = m.group(1)
        return NormalizedPlatformInfo(
            platform_name="Pixiv",
            canonical_url=f"https://www.pixiv.net/artworks/{illust_id}",
            item_id=illust_id,
            badge="🎨 [Pixiv 插画]",
            priority_weight=100,
        )

    m = _PIXIV_USER_REGEX.search(clean_url)
    if m:
        user_id = m.group(1)
        return NormalizedPlatformInfo(
            platform_name="Pixiv",
            canonical_url=f"https://www.pixiv.net/users/{user_id}",
            item_id=user_id,
            badge="🎨 [Pixiv 画师]",
            priority_weight=85,
        )

    # 5. ArtStation
    m = _ARTSTATION_REGEX.search(clean_url)
    if m:
        artwork_id = m.group(1)
        return NormalizedPlatformInfo(
            platform_name="ArtStation",
            canonical_url=f"https://www.artstation.com/artwork/{artwork_id}",
            item_id=artwork_id,
            badge="🖌️ [ArtStation 原画]",
            priority_weight=95,
        )

    # 6. Skeb
    m = _SKEB_REGEX.search(clean_url)
    if m:
        creator, work_id = m.group(1), m.group(2)
        return NormalizedPlatformInfo(
            platform_name="Skeb",
            canonical_url=f"https://skeb.jp/@{creator}/works/{work_id}",
            author=creator,
            item_id=work_id,
            badge="💼 [Skeb 约稿]",
            priority_weight=90,
        )

    # 7. NicoNico
    m = _NICO_SEIGA_REGEX.search(clean_url)
    if m:
        seiga_id = m.group(1)
        return NormalizedPlatformInfo(
            platform_name="NicoNico静画",
            canonical_url=f"https://seiga.nicovideo.jp/seiga/{seiga_id}",
            item_id=seiga_id,
            badge="📐 [Nico静画]",
            priority_weight=85,
        )

    m = _NICO_VIDEO_REGEX.search(clean_url)
    if m:
        nico_id = m.group(1)
        return NormalizedPlatformInfo(
            platform_name="NicoNico动画",
            canonical_url=f"https://www.nicovideo.jp/watch/{nico_id}",
            item_id=nico_id,
            badge="🎞️ [Nico动画]",
            priority_weight=85,
        )

    # 8. 二次元图库 (Danbooru / Gelbooru)
    m = _DANBOORU_REGEX.search(clean_url)
    if m:
        post_id = m.group(1)
        return NormalizedPlatformInfo(
            platform_name="Danbooru",
            canonical_url=f"https://danbooru.donmai.us/posts/{post_id}",
            item_id=post_id,
            badge="📦 [Danbooru 图库]",
            priority_weight=60,
        )

    m = _GELBOORU_REGEX.search(clean_url)
    if m:
        post_id = m.group(1)
        return NormalizedPlatformInfo(
            platform_name="Gelbooru",
            canonical_url=clean_url,
            item_id=post_id,
            badge="📦 [Gelbooru 图库]",
            priority_weight=60,
        )

    # 9. 域名归类 fallback
    domain = urlparse(clean_url).netloc.lower()
    if "weibo.com" in domain or "weibo.cn" in domain:
        return NormalizedPlatformInfo(
            platform_name="微博",
            canonical_url=clean_url,
            badge="📱 [微博]",
            priority_weight=80,
        )
    if "douyin.com" in domain:
        return NormalizedPlatformInfo(
            platform_name="抖音",
            canonical_url=clean_url,
            badge="🎵 [抖音]",
            priority_weight=85,
        )
    if "xiaohongshu.com" in domain or "xhslink.com" in domain:
        return NormalizedPlatformInfo(
            platform_name="小红书",
            canonical_url=clean_url,
            badge="📕 [小红书]",
            priority_weight=85,
        )
    if "miyoushe.com" in domain:
        return NormalizedPlatformInfo(
            platform_name="米游社",
            canonical_url=clean_url,
            badge="🎮 [米游社]",
            priority_weight=85,
        )
    if "lofter.com" in domain:
        return NormalizedPlatformInfo(
            platform_name="LOFTER",
            canonical_url=clean_url,
            badge="📖 [LOFTER]",
            priority_weight=80,
        )
    if "fanbox.cc" in domain:
        return NormalizedPlatformInfo(
            platform_name="Fanbox",
            canonical_url=clean_url,
            badge="🎁 [Fanbox 赞助]",
            priority_weight=90,
        )
    if "pinterest." in domain:
        return NormalizedPlatformInfo(
            platform_name="Pinterest",
            canonical_url=clean_url,
            badge="📌 [Pinterest 二传]",
            priority_weight=20,  # Pinterest 大多是二传转存，降权
        )

    # 通用网页
    display_domain = domain if domain else "网页"
    return NormalizedPlatformInfo(
        platform_name=display_domain,
        canonical_url=clean_url,
        badge=f"🌐 [{display_domain}]",
        priority_weight=40,
    )


_GENERAL_URL_REGEX = re.compile(r"https?://[^\s<>\"'{}|\\^`]+", re.IGNORECASE)


def extract_platform_sources_from_text(text: str) -> List[NormalizedPlatformInfo]:
    """从文本或消息中提取所有互联网链接，并深度解析其平台出处。"""
    if not text or not isinstance(text, str):
        return []

    urls = _GENERAL_URL_REGEX.findall(text)
    if not urls:
        # 兼容用户直接粘贴不带协议头的链接（如 www.bilibili.com/...）
        t = text.strip()
        if t.startswith("www.") or any(dom in t for dom in ("bilibili.com", "x.com", "twitter.com", "youtube.com", "pixiv.net")):
            urls = ["https://" + t]

    results: List[NormalizedPlatformInfo] = []
    seen = set()
    for u in urls:
        clean = u.rstrip(".,;!?，。！？）】>\"')")
        if clean in seen:
            continue
        seen.add(clean)
        norm = normalize_platform_url(clean)
        results.append(norm)

    # 按照原发平台权重降序排序
    results.sort(key=lambda x: x.priority_weight, reverse=True)
    return results


def extract_author_hint(title: str = "", snippet: str = "") -> str:
    """从网页标题或摘要中尽可能提取作者/UP主名。"""
    combined = f"{title} | {snippet}"
    # 匹配 "视频作者: xxx", "作者: xxx", "UP主: xxx", "画师: xxx", "Coser: xxx"
    m = re.search(r"(?:视频作者|作者|UP主|up主|画师|摄影|coser|Coser)[：:\s]+([a-zA-Z0-9_\u4e00-\u9fa5]{2,20})", combined)
    if m:
        cand = m.group(1).strip()
        if cand.lower() not in ("分享", "合集", "盘点", "动漫", "原神", "星穹铁道", "bilibili", "作品", "视频", "资讯"):
            return cand

    # 匹配 "xxx于2024...发布在抖音" 或 "xxx · 1年前"
    m = re.search(r"([a-zA-Z0-9_\u4e00-\u9fa5]{2,16})\s*(?:于\d{4,8}发布在抖音|·\s*\d+年前|的微博|的个人主页)", combined)
    if m:
        cand = m.group(1).strip()
        if cand.lower() not in ("分享", "合集", "盘点", "抖音", "微博"):
            return cand

    # 匹配 "xxx on X:"
    m = re.search(r"([a-zA-Z0-9_]{2,20})\s+on\s+X:", combined)
    if m:
        return m.group(1).strip()

    # 匹配 @handle
    m = re.search(r"@([a-zA-Z0-9_]{3,20})", combined)
    if m:
        cand = m.group(1).strip()
        if cand.lower() not in ("bilibili", "youtube", "twitter", "douyin"):
            return cand

    return ""
