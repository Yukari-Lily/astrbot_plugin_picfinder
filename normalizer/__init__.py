"""URL 与平台规范化模块。"""

from .author_extractor import (
    ArtworkSample,
    AuthorArtworksManager,
    AuthorProfile,
)
from .platform_matcher import (
    NormalizedPlatformInfo,
    extract_platform_sources_from_text,
    normalize_platform_url,
)

__all__ = [
    "NormalizedPlatformInfo",
    "normalize_platform_url",
    "extract_platform_sources_from_text",
    "AuthorProfile",
    "ArtworkSample",
    "AuthorArtworksManager",
]
