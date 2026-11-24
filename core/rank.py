"""候选结果打分、去重与排序。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

try:
    from ..engines.base import Candidate  # type: ignore
except (ImportError, ValueError):  # pragma: no cover
    from engines.base import Candidate  # type: ignore

from .platform import classify, clean_url, image_key, is_blocked_url, is_listing_url


@dataclass
class Ranked:
    primary: Optional[Candidate] = None
    alternates: List[Candidate] = field(default_factory=list)
    total: int = 0
    exact_found: bool = False

    @property
    def empty(self) -> bool:
        return self.primary is None


def score(candidate: Candidate) -> float:
    info = classify(candidate.url)
    value = float(info.weight)
    if candidate.verified:
        value += 60
    elif candidate.trusted:
        value += 30
    elif candidate.exact:
        value += 15
    else:
        # 只是「相似」时，平台再好也不能当成出处，整体减半
        value *= 0.55
    if candidate.engine == "SauceNAO":
        value += min(candidate.similarity, 100) / 20
    elif candidate.engine == "TraceMoe":
        value += min(candidate.similarity, 100) / 20
    elif candidate.engine == "Google Lens":
        value += 3
    verify_state = candidate.extra.get("verify")
    if verify_state == "near":
        value -= 6
    elif verify_state == "mismatch":
        value -= 45
    if info.kind == "pinterest":
        value -= 20
    return value


def _who(candidate: Candidate) -> str:
    identity = classify(candidate.url)
    return (candidate.author or identity.author_hint or "").strip().lower()


def rank_candidates(candidates: List[Candidate], limit: int = 3) -> Ranked:
    limit = max(1, int(limit))
    best_by_url: dict = {}

    for candidate in candidates:
        if not candidate or not candidate.url:
            continue
        candidate.url = clean_url(candidate.url)
        if is_blocked_url(candidate.url) or is_listing_url(candidate.url):
            continue
        info = classify(candidate.url)
        candidate.url = info.canonical or candidate.url
        if not candidate.platform:
            candidate.platform = info.name
        if not candidate.author and info.author_hint:
            candidate.author = info.author_hint

        key = candidate.url
        existing = best_by_url.get(key)
        if existing is None or score(candidate) > score(existing):
            best_by_url[key] = candidate

    # 同一张图只保留得分最高的一条，其余记为「转载」
    best_by_image: dict = {}
    mirrors: dict = {}
    for candidate in best_by_url.values():
        key = image_key(candidate.image)
        if not key:
            key = "url:" + candidate.url
            best_by_image.setdefault(key, candidate)
        elif key in best_by_image:
            best = best_by_image[key]
            count = mirrors.pop(id(best), 0) + 1
            if score(candidate) > score(best):
                best = candidate
                best_by_image[key] = candidate
            mirrors[id(best)] = count
        else:
            best_by_image[key] = candidate

    ordered = sorted(best_by_image.values(), key=score, reverse=True)
    for candidate in ordered:
        count = mirrors.get(id(candidate), 0)
        if count:
            candidate.extra["mirrors"] = count

    primary = ordered[0] if ordered else None
    alternates: List[Candidate] = []
    seen_identity = {(_who(primary), primary.platform) if primary else None}
    for candidate in ordered[1:]:
        if len(alternates) >= limit - 1:
            break
        if not (candidate.verified or candidate.trusted):
            continue
        if classify(candidate.url).weight < 60:
            continue
        identity = (_who(candidate), candidate.platform)
        if identity in seen_identity:
            continue
        seen_identity.add(identity)
        alternates.append(candidate)

    exact_found = any(c.trusted for c in ordered[:3])
    return Ranked(primary=primary, alternates=alternates, total=len(ordered), exact_found=exact_found)
