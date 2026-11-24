"""搜图引擎的通用数据模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List


@dataclass

class Candidate:
    """一条候选出处。"""

    url: str
    title: str = ""
    platform: str = ""
    author: str = ""
    author_url: str = ""
    date: str = ""
    image: str = ""  # 页面内对应原图的直链
    thumb: str = ""
    engine: str = ""  # 发现它的引擎
    exact: bool = False  # 引擎声称是精确匹配
    verified: bool = False  # 已用 dHash 核对为同一张图
    similarity: float = 0.0
    extra: dict = field(default_factory=dict)

    @property
    def trusted(self) -> bool:
        """没有 dHash 复核时，只信引擎本身的精确匹配能力。"""
        if self.verified:
            return True
        return self.exact and self.engine in {"Google Lens", "SauceNAO", "TraceMoe"}


@dataclass

class EngineResult:
    engine: str
    candidates: List[Candidate] = field(default_factory=list)
    error: str = ""
    elapsed: float = 0.0

    def add(self, candidate: Candidate) -> None:
        candidate.engine = candidate.engine or self.engine
        self.candidates.append(candidate)
