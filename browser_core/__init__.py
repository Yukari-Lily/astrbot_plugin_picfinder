"""Shared Playwright browser core used by astrbot_plugin_browser and picfinder."""

from .manager import (
    BrowserConfig,
    BrowserError,
    BrowserManager,
    BrowserPage,
    BrowserResponse,
    Snapshot,
    evaluate_to_text,
    format_snapshot,
    is_blank_page,
)
from .urls import UrlRejected, normalize_url

__all__ = [
    "BrowserConfig",
    "BrowserError",
    "BrowserManager",
    "BrowserPage",
    "BrowserResponse",
    "Snapshot",
    "UrlRejected",
    "is_blank_page",
    "normalize_url",
    "format_snapshot",
    "evaluate_to_text",
]
