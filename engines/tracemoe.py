"""TraceMoe 动漫画面识别引擎（第几集、第几秒）。"""

from __future__ import annotations

import time

try:
    from ..core.image import ImageInput  # type: ignore
except (ImportError, ValueError):  # pragma: no cover
    from core.image import ImageInput  # type: ignore
from .base import Candidate, EngineResult


def _upload_parts(img: ImageInput):
    data, mime, filename = img.upload_parts()
    return filename, data, mime or "image/jpeg"


_ENDPOINT = "https://api.trace.moe/search"


class TraceMoeEngine:
    engine_name = "TraceMoe"

    def __init__(self, client, *, timeout: float = 25.0):
        self.client = client
        self.timeout = timeout

    async def search(self, img: ImageInput, limit: int = 3) -> EngineResult:
        started = time.monotonic()
        result = EngineResult(engine=self.engine_name)
        try:
            resp = await self.client.post(
                _ENDPOINT,
                params={"anilistInfo": "1"},
                files={"file": (*_upload_parts(img),)},
                timeout=self.timeout,
            )
            if resp.status != 200:
                raise RuntimeError(f"HTTP {resp.status}")
            payload = resp.json()
            if payload.get("error"):
                raise RuntimeError(str(payload["error"])[:120])
            for entry in (payload.get("result") or [])[: max(1, limit)]:
                similarity = float(entry.get("similarity", 0.0)) * 100
                if similarity < 80:
                    continue
                title = self._title_of(entry)
                episode = entry.get("episode")
                seconds = int(entry.get("from") or 0)
                timecode = f"{seconds // 60:02d}:{seconds % 60:02d}"
                label = f"{title}" + (f" 第 {episode} 集" if episode else "") + f" {timecode}"
                page_url = "https://trace.moe/"
                if img.source_url:
                    from urllib.parse import quote

                    page_url = f"https://trace.moe/?url={quote(img.source_url, safe='')}"
                result.candidates.append(
                    Candidate(
                        url=page_url,
                        title=label.strip(),
                        platform="动画番剧",
                        image=str(entry.get("image") or ""),
                        thumb=str(entry.get("image") or ""),
                        engine=self.engine_name,
                        exact=similarity >= 88,
                        similarity=similarity,
                        extra={"episode": episode, "time": timecode},
                    )
                )
            if not result.candidates:
                result.error = "无匹配结果"
        except Exception as exc:
            result.error = str(exc)[:160]
        result.elapsed = time.monotonic() - started
        return result

    @staticmethod
    def _title_of(entry: dict) -> str:
        anilist = entry.get("anilist") or {}
        title = anilist.get("title") or {}
        if isinstance(title, dict):
            for key in ("chinese", "romaji", "english", "native"):
                value = title.get(key)
                if value:
                    return str(value)
        filename = entry.get("filename") or "动漫截图"
        return str(filename)
