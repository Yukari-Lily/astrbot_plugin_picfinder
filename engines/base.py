"""以图搜图通用数据模型与基类定义。"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class SearchResultItem:
    """标准化搜图单项结果"""
    title: str = ""
    url: str = ""
    platform: str = "网页"  # 识别出的平台名，如 X (Twitter), 哔哩哔哩, YouTube, Pixiv, Danbooru
    author: str = ""
    thumbnail: str = ""
    similarity: float = 0.0  # 相似度 (0 - 100)
    source_engine: str = ""  # 来源引擎 (Google Lens, SauceNAO, Ascii2D, TraceMoe)
    is_exact_match: bool = False  # 是否为视觉完全匹配/高确信度原图
    extra_info: Dict[str, Any] = field(default_factory=dict)  # 额外元数据 (如集数, 时间戳, pixiv_id等)

    def to_summary_line(self) -> str:
        """生成单行摘要"""
        parts = []
        if self.platform and self.platform != "网页":
            parts.append(f"[{self.platform}]")
        if self.author:
            parts.append(f"@{self.author}")
        if self.title:
            parts.append(f"「{self.title}」")
        if self.similarity > 0:
            parts.append(f"(相似度: {self.similarity:.1f}%)")
        if self.url:
            parts.append(f"-> {self.url}")
        return " ".join(parts)


@dataclass
class EngineSearchResult:
    """单个搜索引擎的完整检索响应结果"""
    engine_name: str
    items: List[SearchResultItem] = field(default_factory=list)
    error: Optional[str] = None
    best_similarity: float = 0.0

    @property
    def has_results(self) -> bool:
        return len(self.items) > 0
