"""平台识别、URL 规范化与来源权重。

只做「这是哪个平台、原帖链接是什么、值不值得信」这三件事，
不再做易错的跨平台抓取或关键词猜测。
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from urllib.parse import parse_qs, parse_qsl, unquote, urlencode, urlsplit, urlunsplit

# ---------------------------------------------------------------------------
# 正则
# ---------------------------------------------------------------------------
_X_STATUS = re.compile(
    r"https?://(?:www\.|mobile\.)?(?:x|twitter)\.com/([A-Za-z0-9_]{1,30})/status/(\d+)",
    re.I,
)
_X_PROFILE = re.compile(r"https?://(?:www\.|mobile\.)?(?:x|twitter)\.com/([A-Za-z0-9_]{1,30})/?(?:[?#].*)?$", re.I)
_PIXIV_ART = re.compile(r"https?://(?:www\.)?pixiv\.net/(?:[a-z]{2}/)?artworks/(\d+)", re.I)
_PIXIV_LEGACY = re.compile(r"https?://(?:www\.)?pixiv\.net/(?:member_illust\.php)?\?(?:[^#]*&)?illust_id=(\d+)", re.I)
_PIXIV_SHORT = re.compile(r"https?://(?:www\.)?pixiv\.net/(?:[a-z]{2}/)?i/(\d+)", re.I)
_PIXIV_USER = re.compile(r"https?://(?:www\.)?pixiv\.net/(?:[a-z]{2}/)?users/(\d+)", re.I)
_BILI_VIDEO = re.compile(r"https?://(?:www\.|m\.)?bilibili\.com/video/(BV[0-9A-Za-z]{10}|av\d+)", re.I)
_BILI_OPUS = re.compile(r"https?://(?:www\.)?bilibili\.com/opus/(\d+)", re.I)
_BILI_DYNAMIC = re.compile(r"https?://t\.bilibili\.com/(\d+)", re.I)
_BILI_READ = re.compile(r"https?://(?:www\.)?bilibili\.com/read/cv(\d+)", re.I)
_YOUTUBE = re.compile(
    r"https?://(?:www\.)?(?:youtube\.com/(?:watch\?(?:[^#]*?&)?v=|shorts/|embed/)|youtu\.be/)([0-9A-Za-z_-]{11})",
    re.I,
)
_DANBOORU = re.compile(r"https?://danbooru\.donmai\.us/posts/(\d+)", re.I)
_GELBOORU = re.compile(r"https?://(?:www\.)?gelbooru\.com/index\.php\?[^#]*\bid=(\d+)", re.I)
_ARTSTATION = re.compile(r"https?://(?:www\.)?artstation\.com/artwork/([0-9A-Za-z_-]+)", re.I)
_SKEB = re.compile(r"https?://skeb\.jp/@([0-9A-Za-z_-]+)/works/(\d+)", re.I)
_FANBOX = re.compile(r"https?://([0-9A-Za-z_-]+)\.fanbox\.cc/posts/(\d+)", re.I)
_WEIBO_STATUS = re.compile(r"https?://(?:www\.|m\.)?weibo\.(?:com|cn)/(?:\d+|detail)/(\d+)", re.I)
_DOUYIN = re.compile(r"https?://(?:www\.)?douyin\.com/(?:note|video)/(\d+)", re.I)
_XHS = re.compile(r"https?://(?:www\.)?xiaohongshu\.com/(?:explore|discovery/item)/([0-9A-Za-z]+)", re.I)
_MIYOUSHE = re.compile(r"https?://(?:www\.)?miyoushe\.com/[^/]+/article/(\d+)", re.I)
_LOFTER = re.compile(r"https?://[0-9A-Za-z_-]+\.lofter\.com/post/([0-9A-Za-z_]+)", re.I)
_DEVIANTART = re.compile(r"https?://(?:www\.)?deviantart\.com/(?:[^/]+/)?(?:art|view)/([0-9A-Za-z_-]+)", re.I)
_PINTEREST = re.compile(r"https?://(?:[a-z]{2}\.)?pinterest\.(?:com|co\.[a-z]{2}|de|fr|jp)/pin/(\d+)", re.I)

_GOOGLE_HOSTS = ("google.com", "google.com.hk", "google.co.jp", "gstatic.com", "googleusercontent.com")

_TRACKING_KEYS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "utm_id",
    "fbclid",
    "gclid",
    "yclid",
    "mc_cid",
    "mc_eid",
    "igshid",
    "ref_src",
    "ref_url",
    "share_token",
    "spm_id_from",
    "vd_source",
}

# 平台权重：原发/高可信平台在前，聚合站/二传站大幅降权
_PLATFORM_WEIGHTS = {
    "Pixiv": 100,
    "X": 96,
    "Skeb": 94,
    "Fanbox": 92,
    "ArtStation": 90,
    "Bilibili": 88,
    "YouTube": 86,
    "Danbooru": 82,
    "DeviantArt": 68,
    "Miyoushe": 82,
    "Weibo": 80,
    "Gelbooru": 78,
    "Xiaohongshu": 76,
    "Douyin": 74,
    "Lofter": 72,
    "NicoNico": 70,
    "Pinterest": 18,
}

_BLOCKED_HOSTS = (
    "google.com",
    "google.com.hk",
    "google.co.jp",
    "googleusercontent.com",
    "gstatic.com",
    "doubleclick.net",
    "googlesyndication.com",
    "schema.org",
    "w3.org",
)


@dataclass(frozen=True)

class PlatformInfo:
    name: str
    kind: str
    weight: int
    canonical: str = ""
    item_id: str = ""
    author_hint: str = ""


def normalize_space(text: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(text or "")).strip()


def domain_of(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def unwrap_url(url: str) -> str:
    """展开 Google 跳转链接，去掉常见包装。"""
    url = (url or "").strip()
    if not url:
        return ""
    if url.startswith("//"):
        url = "https:" + url
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    host = (parts.hostname or "").lower()
    if host.endswith("google.com") or host.endswith("google.com.hk"):
        query = parse_qs(parts.query)
        for key in ("url", "q", "imgurl", "u"):
            values = query.get(key)
            if values:
                candidate = unquote(values[0])
                if candidate.startswith(("http://", "https://")):
                    return candidate
    return url


def strip_tracking(url: str) -> str:
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    if not parts.query:
        return url
    kept = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k.lower() not in _TRACKING_KEYS]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(kept, doseq=True), ""))


def clean_url(url: str) -> str:
    return strip_tracking(unwrap_url(url)).strip()


def classify(url: str) -> PlatformInfo:
    raw = clean_url(url)
    if not raw:
        return PlatformInfo("网页", "generic", 0)

    match = _X_STATUS.search(raw)
    if match:
        handle, status_id = match.group(1), match.group(2)
        if handle.lower() not in {"i", "intent", "search", "explore", "home", "hashtag"}:
            return PlatformInfo(
                "X",
                "x",
                _PLATFORM_WEIGHTS["X"],
                canonical=f"https://x.com/{handle}/status/{status_id}",
                item_id=status_id,
                author_hint=handle,
            )

    match = _X_PROFILE.search(raw)
    if match:
        handle = match.group(1)
        if handle.lower() not in {"i", "intent", "search", "explore", "home", "hashtag", "settings", "login"}:
            return PlatformInfo(
                "X",
                "x_user",
                60,
                canonical=f"https://x.com/{handle}",
                author_hint=handle,
            )

    match = _PIXIV_ART.search(raw) or _PIXIV_LEGACY.search(raw) or _PIXIV_SHORT.search(raw)
    if match:
        return PlatformInfo(
            "Pixiv",
            "pixiv",
            _PLATFORM_WEIGHTS["Pixiv"],
            canonical=f"https://www.pixiv.net/artworks/{match.group(1)}",
            item_id=match.group(1),
        )
    match = _PIXIV_USER.search(raw)
    if match:
        return PlatformInfo(
            "Pixiv",
            "pixiv_user",
            60,
            canonical=f"https://www.pixiv.net/users/{match.group(1)}",
            item_id=match.group(1),
        )

    match = _BILI_VIDEO.search(raw)
    if match:
        return PlatformInfo(
            "Bilibili",
            "bilibili",
            _PLATFORM_WEIGHTS["Bilibili"],
            canonical=f"https://www.bilibili.com/video/{match.group(1)}",
            item_id=match.group(1),
        )
    match = _BILI_OPUS.search(raw) or _BILI_DYNAMIC.search(raw)
    if match:
        return PlatformInfo(
            "Bilibili",
            "bilibili",
            80,
            canonical=f"https://www.bilibili.com/opus/{match.group(1)}",
            item_id=match.group(1),
        )
    match = _BILI_READ.search(raw)
    if match:
        return PlatformInfo(
            "Bilibili",
            "bilibili",
            75,
            canonical=f"https://www.bilibili.com/read/cv{match.group(1)}",
            item_id=f"cv{match.group(1)}",
        )

    match = _YOUTUBE.search(raw)
    if match:
        return PlatformInfo(
            "YouTube",
            "youtube",
            _PLATFORM_WEIGHTS["YouTube"],
            canonical=f"https://www.youtube.com/watch?v={match.group(1)}",
            item_id=match.group(1),
        )

    match = _DANBOORU.search(raw)
    if match:
        return PlatformInfo(
            "Danbooru",
            "danbooru",
            _PLATFORM_WEIGHTS["Danbooru"],
            canonical=f"https://danbooru.donmai.us/posts/{match.group(1)}",
            item_id=match.group(1),
        )
    match = _GELBOORU.search(raw)
    if match:
        return PlatformInfo(
            "Gelbooru",
            "gelbooru",
            _PLATFORM_WEIGHTS["Gelbooru"],
            canonical=raw,
            item_id=match.group(1),
        )

    match = _ARTSTATION.search(raw)
    if match:
        return PlatformInfo(
            "ArtStation",
            "artstation",
            _PLATFORM_WEIGHTS["ArtStation"],
            canonical=f"https://www.artstation.com/artwork/{match.group(1)}",
            item_id=match.group(1),
        )

    match = _SKEB.search(raw)
    if match:
        return PlatformInfo(
            "Skeb",
            "skeb",
            _PLATFORM_WEIGHTS["Skeb"],
            canonical=f"https://skeb.jp/@{match.group(1)}/works/{match.group(2)}",
            item_id=match.group(2),
            author_hint=match.group(1),
        )

    match = _FANBOX.search(raw)
    if match:
        return PlatformInfo(
            "Fanbox",
            "fanbox",
            _PLATFORM_WEIGHTS["Fanbox"],
            canonical=f"https://{match.group(1)}.fanbox.cc/posts/{match.group(2)}",
            item_id=match.group(2),
            author_hint=match.group(1),
        )

    match = _WEIBO_STATUS.search(raw)
    if match:
        return PlatformInfo("Weibo", "weibo", _PLATFORM_WEIGHTS["Weibo"], canonical=raw, item_id=match.group(1))

    match = _DOUYIN.search(raw)
    if match:
        return PlatformInfo("Douyin", "douyin", _PLATFORM_WEIGHTS["Douyin"], canonical=raw, item_id=match.group(1))

    match = _XHS.search(raw)
    if match:
        return PlatformInfo(
            "Xiaohongshu",
            "xiaohongshu",
            _PLATFORM_WEIGHTS["Xiaohongshu"],
            canonical=f"https://www.xiaohongshu.com/explore/{match.group(1)}",
            item_id=match.group(1),
        )

    match = _MIYOUSHE.search(raw)
    if match:
        return PlatformInfo(
            "Miyoushe", "miyoushe", _PLATFORM_WEIGHTS["Miyoushe"], canonical=raw, item_id=match.group(1)
        )

    match = _LOFTER.search(raw)
    if match:
        return PlatformInfo("Lofter", "lofter", _PLATFORM_WEIGHTS["Lofter"], canonical=raw, item_id=match.group(1))

    match = _DEVIANTART.search(raw)
    if match:
        return PlatformInfo(
            "DeviantArt",
            "deviantart",
            _PLATFORM_WEIGHTS["DeviantArt"],
            canonical=raw,
            item_id=match.group(1),
        )

    match = _PINTEREST.search(raw)
    if match:
        return PlatformInfo(
            "Pinterest",
            "pinterest",
            _PLATFORM_WEIGHTS["Pinterest"],
            canonical=raw,
            item_id=match.group(1),
        )

    host = domain_of(raw)
    if not host:
        return PlatformInfo("网页", "generic", 0, canonical=raw)
    if host.endswith("nicovideo.jp"):
        return PlatformInfo("NicoNico", "niconico", _PLATFORM_WEIGHTS["NicoNico"], canonical=raw)
    return PlatformInfo(host.replace("www.", ""), "generic", 40, canonical=raw)


_LISTING_PATTERNS = (
    re.compile(r"danbooru\.donmai\.us/posts\?", re.I),
    re.compile(r"gelbooru\.com/index\.php\?.*s=list", re.I),
    re.compile(r"/search\?", re.I),
    re.compile(r"[?&]page=\d+", re.I),
)


def is_listing_url(url: str) -> bool:
    """过滤「搜索结果/列表页」，它们不是某张图的原帖。"""
    if not url:
        return False
    if re.search(r"/(?:artworks|status|posts|video|watch)/", url, re.I):
        return False
    return any(pattern.search(url) for pattern in _LISTING_PATTERNS)


def is_blocked_url(url: str) -> bool:
    url = (url or "").strip()
    if not url.startswith(("http://", "https://")):
        return True
    host = domain_of(url)
    if not host or "." not in host:
        return True
    if any(host == blocked or host.endswith("." + blocked) for blocked in _BLOCKED_HOSTS):
        return True
    return False


def image_key(url: str) -> str:
    """把同一张图的不同尺寸/参数 URL 归一为一个 key。"""
    if not url:
        return ""
    try:
        parts = urlsplit(unwrap_url(url))
    except ValueError:
        return url
    path = re.sub(r"/(?:\d+x\d*|\d*x\d+)/", "/", parts.path)
    path = re.sub(r"/(?:originals|orig|preview|thumb|thumbnail|small|medium|large)/", "/", path)
    path = re.sub(r"![a-z]+$", "", path, flags=re.I)
    path = re.sub(r"_(?:normal|mini|thumb|small|medium|large|\d+x\d+)(?=\.\w+$)", "", path)
    path = re.sub(r"\.(?:jpe?g|png|webp|gif)$", "", path, flags=re.I)
    path = re.sub(r"/(?:preview|thumb|thumbnail)/", "/", path)
    return f"{parts.netloc.lower()}{path}".lower()


def first_line(text: str, limit: int = 90) -> str:
    if not text:
        return ""
    for raw_line in re.split(r"[\r\n]+", str(text)):
        cleaned = normalize_space(raw_line)
        if cleaned:
            return cleaned[:limit].rstrip(" -–—|·")
    return ""
