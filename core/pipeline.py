"""检索流水线：并发跑引擎 → 补全出处 → 指纹核对 → 排序。"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Dict, List

try:
    from astrbot.api import logger  # type: ignore
except Exception:  # pragma: no cover
    logger = logging.getLogger("astrbot_plugin_picfinder.pipeline")

try:
    from ..engines.ascii2d import Ascii2DEngine  # type: ignore
    from ..engines.base import Candidate  # type: ignore
    from ..engines.lens import LensEngine  # type: ignore
    from ..engines.saucenao import SauceNAOEngine  # type: ignore
    from ..engines.tracemoe import TraceMoeEngine  # type: ignore
    from ..engines.yandex import YandexEngine  # type: ignore
except (ImportError, ValueError):  # pragma: no cover
    from engines.ascii2d import Ascii2DEngine  # type: ignore
    from engines.base import Candidate  # type: ignore
    from engines.lens import LensEngine  # type: ignore
    from engines.saucenao import SauceNAOEngine  # type: ignore
    from engines.tracemoe import TraceMoeEngine  # type: ignore
    from engines.yandex import YandexEngine  # type: ignore
try:
    from ..browser_core import BrowserConfig, BrowserManager  # type: ignore
except (ImportError, ValueError):  # pragma: no cover
    from browser_core import BrowserConfig, BrowserManager  # type: ignore
from .image import ImageInput
from .net import HttpClient
from .rank import Ranked, rank_candidates
from .resolve import SourceResolver


@dataclass
class PipelineConfig:
    lens_backend: str = "auto"
    lens_safe_search: bool = False
    lens_headless: bool = True
    lens_profile_dir: str = ""
    lens_browser_channel: str = ""
    lens_include_visual: bool = True
    enable_yandex: bool = True
    yandex_family_filter: bool = False
    enable_saucenao: bool = True
    enable_ascii2d: bool = True
    ascii2d_bovw: bool = True
    saucenao_api_key: str = ""
    saucenao_threshold: float = 65.0
    enable_tracemoe: bool = True
    verify_sources: bool = True
    max_results: int = 3
    engine_timeout: float = 70.0
    proxy: str = ""
    browser_executable_path: str = ""
    browser_user_data_dir: str = ""
    browser_timeout: float = 30.0


@dataclass
class Report:
    ranked: Ranked
    candidates: List[Candidate] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    stats: Dict[str, str] = field(default_factory=dict)
    elapsed: float = 0.0


class Pipeline:
    def __init__(self, config: PipelineConfig, browser: BrowserManager | None = None):
        self.config = config
        self.client = HttpClient(proxy=config.proxy, timeout=20.0)
        self.browser = browser
        self.browser_error = ""
        if self.browser is None:
            self.browser = BrowserManager(
                BrowserConfig(
                    executable_path=config.browser_executable_path,
                    user_data_dir=config.browser_user_data_dir or config.lens_profile_dir,
                    headless=config.lens_headless,
                    proxy=config.proxy,
                    channel=config.lens_browser_channel,
                    viewport_width=1440,
                    viewport_height=960,
                    navigation_timeout_ms=int(config.browser_timeout * 1000),
                    action_timeout_ms=15000,
                )
            )
        self.lens = LensEngine(
            self.client,
            backend=config.lens_backend,
            safe_search=config.lens_safe_search,
            include_visual=config.lens_include_visual,
            timeout=config.engine_timeout,
            browser=self.browser,
            browser_error=self.browser_error,
        )
        self.yandex = YandexEngine(self.client, family_filter=config.yandex_family_filter)
        self.ascii2d = Ascii2DEngine(
            self.client,
            bovw=config.ascii2d_bovw,
            timeout=20.0,
            browser=self.browser,
            browser_error=self.browser_error,
        )
        self.saucenao = SauceNAOEngine(
            self.client,
            api_key=config.saucenao_api_key,
            threshold=config.saucenao_threshold,
        )
        self.tracemoe = TraceMoeEngine(self.client)
        self.resolver = SourceResolver(self.client, verify=config.verify_sources)

    async def search(self, img: ImageInput) -> Report:
        started = time.monotonic()
        jobs = []

        if self.config.lens_backend != "off":
            jobs.append(("Google Lens", self.lens.search(img, 8), self.config.engine_timeout + 5))
        if self.config.enable_yandex:
            jobs.append(("Yandex", self.yandex.search(img, 12), 30.0))
        if self.config.enable_saucenao:
            jobs.append(("SauceNAO", self.saucenao.search(img, 8), 25.0))
        if self.config.enable_ascii2d:
            jobs.append(("Ascii2D", self.ascii2d.search(img, 2), 25.0))
        if self.config.enable_tracemoe:
            jobs.append(("TraceMoe", self.tracemoe.search(img, 3), 20.0))

        async def run(name: str, coro, timeout: float):
            try:
                return await asyncio.wait_for(coro, timeout=timeout)
            except asyncio.TimeoutError:
                return _failed(name, "超时")
            except Exception as exc:  # pragma: no cover - 防御
                return _failed(name, str(exc)[:120])

        results = await asyncio.gather(*(run(*job) for job in jobs))
        candidates: List[Candidate] = []
        errors: List[str] = []
        stats: Dict[str, str] = {}
        for result in results:
            candidates.extend(result.candidates)
            if result.error:
                errors.append(f"{result.engine}: {result.error}")
                logger.warning(f"picfinder: {result.engine} 失败: {result.error}")
            stats[result.engine] = f"{len(result.candidates)} 条 / {result.elapsed:.1f}s"

        if candidates:
            candidates = await self.resolver.enrich(candidates, img)
        ranked = rank_candidates(candidates, limit=self.config.max_results)
        report = Report(
            ranked=ranked,
            candidates=candidates,
            errors=errors,
            stats=stats,
            elapsed=time.monotonic() - started,
        )
        logger.info(
            f"picfinder: {len(candidates)} 条候选，"
            f"主来源={ranked.primary.url if ranked.primary else '无'}，"
            f"用时 {report.elapsed:.1f}s"
        )
        return report

    async def close(self) -> None:
        if self.browser is not None:
            try:
                await self.browser.close()
            except Exception:
                pass
        await self.client.close()


def _failed(name: str, reason: str):
    try:
        from ..engines.base import EngineResult  # type: ignore
    except (ImportError, ValueError):  # pragma: no cover
        from engines.base import EngineResult  # type: ignore

    return EngineResult(engine=name, error=reason)
