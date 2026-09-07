"""Extractor registry: try each extractor, return the first that matches.

Playwright (headless Chromium) is required for Cloudflare-protected sites
(altadefinizione, streamingcommunity). On environments where Playwright or
Chromium is not available (e.g. an Android phone running the bot via Termux),
the registry degrades gracefully: get_extractor() returns None and the bot
falls back to yt-dlp directly (YouTube, TikTok, etc. still work; the Italian
streaming sites won't, until Playwright+Chromium are installed).
"""

from __future__ import annotations

import os

from bot.extractors.base import BaseExtractor, PlaywrightVideoExtractor, VideoInfo
from bot.extractors.beeg import BeegExtractor
from bot.extractors.hentaiworld import HentaiWorldExtractor
from bot.extractors.internetchicks import InternetchicksExtractor
from bot.extractors.porn4fans import Porn4FansExtractor
from bot.extractors.surrit import SurritExtractor
from bot.extractors.tube8 import Tube8Extractor
from bot.logging_config import get_logger

_log = get_logger("extractor")

_PLAYWRIGHT_AVAILABLE = True
try:
    import playwright  # noqa: F401
except ImportError:
    _PLAYWRIGHT_AVAILABLE = False

_EXTRACTORS: list[type[BaseExtractor]] = [
    HentaiWorldExtractor,
    Tube8Extractor,
    BeegExtractor,
    Porn4FansExtractor,
    SurritExtractor,
    InternetchicksExtractor,
]

if _PLAYWRIGHT_AVAILABLE:
    from bot.extractors.altadefinizione import AltaDefinizioneExtractor
    from bot.extractors.streamingcommunity import StreamingCommunityExtractor
    from bot.extractors.supjav import SupjavExtractor
    from bot.extractors.xinindia import XinIndiaExtractor
    _EXTRACTORS.extend([
        AltaDefinizioneExtractor,
        StreamingCommunityExtractor,
        SupjavExtractor,
        XinIndiaExtractor,
    ])


def get_universal_fallback(url: str) -> BaseExtractor | None:
    """Fallback browser-based extractor for free tube sites whose yt-dlp
    extractor is broken/outdated. Used only AFTER yt-dlp fails, so sites with
    a working yt-dlp extractor never pay the browser cost."""
    if not _PLAYWRIGHT_AVAILABLE:
        return None
    from bot.extractors.universal import (
        UniversalPlaywrightExtractor,
        XVideoTubeExtractor,
    )
    for cls in (XVideoTubeExtractor, UniversalPlaywrightExtractor):
        if cls.can_handle(url):
            return cls()
    return None


def is_playwright_ready() -> bool:
    """True if both the playwright package and a Chromium browser are usable.

    We check the package import (already done) plus whether the chromium
    browser binary is installed (playwright install chromium). On a fresh
    Termux install the package may be present but the browser not yet, so
    this distinguishes the two states for a helpful error message.
    """
    if not _PLAYWRIGHT_AVAILABLE:
        return False
    # Heuristic: playwright stores browsers under MSYSTEM/playwright. We just
    # try a quick check via the sync API launch. That's expensive, so instead
    # check the common install path. If we can't tell, assume ready.
    for env_name in ("PLAYWRIGHT_BROWSERS_PATH",):
        p = os.environ.get(env_name)
        if p and os.path.isdir(p):
            return True
    # Default cache locations.
    home = os.path.expanduser("~")
    for candidate in (
        os.path.join(home, ".cache", "ms-playwright"),
        os.path.join(home, "AppData", "Local", "ms-playwright"),
        os.path.join(home, ".cache", "playwright"),
    ):
        if not os.path.isdir(candidate):
            continue
        try:
            entries = os.listdir(candidate)
        except OSError:
            continue
        if any(os.path.isdir(os.path.join(candidate, d)) and "chromium" in d.lower()
               for d in entries):
            return True
    # Couldn't confirm a browser install; be optimistic so we still attempt
    # extraction (the launch will raise a clear error if missing).
    return True


def get_extractor(url: str) -> BaseExtractor | None:
    """Return an extractor instance that can handle `url`, or None.

    If the extractor requires Playwright and it's not installed, returns None
    and logs a warning.
    """
    for cls in _EXTRACTORS:
        if cls.can_handle(url):
            if issubclass(cls, PlaywrightVideoExtractor) and not _PLAYWRIGHT_AVAILABLE:
                _log.warning(
                    f"L'estrattore per {cls.__name__} richiede Playwright, "
                    "che non è installato. Esegui: pip install playwright && playwright install chromium"
                )
                return None
            return cls()
    return None


__all__ = ["BaseExtractor", "PlaywrightVideoExtractor", "VideoInfo",
           "get_extractor", "get_universal_fallback", "is_playwright_ready"]
