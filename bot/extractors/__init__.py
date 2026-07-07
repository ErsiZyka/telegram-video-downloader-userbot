"""Extractor registry: try each extractor, return the first that matches.

Playwright (headless Chromium) is required for Cloudflare-protected sites
(altadefinizione, streamingcommunity). On environments where Playwright or
Chromium is not available (e.g. an Android phone running the bot via Termux),
the registry degrades gracefully: get_extractor() returns None and the bot
falls back to yt-dlp directly (YouTube, TikTok, etc. still work; the Italian
streaming sites won't, until Playwright+Chromium are installed).
"""

from __future__ import annotations

import shutil

from bot.extractors.base import BaseExtractor, PlaywrightVideoExtractor, VideoInfo
from bot.logging_config import get_logger

_log = get_logger("extractor")

_PLAYWRIGHT_AVAILABLE = True
try:
    import playwright  # noqa: F401
except ImportError:
    _PLAYWRIGHT_AVAILABLE = False

# Order matters: more specific first.
_EXTRACTORS: list[type[BaseExtractor]] = []
if _PLAYWRIGHT_AVAILABLE:
    from bot.extractors.altadefinizione import AltaDefinizioneExtractor
    from bot.extractors.streamingcommunity import StreamingCommunityExtractor
    _EXTRACTORS = [
        AltaDefinizioneExtractor,
        StreamingCommunityExtractor,
    ]


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
    import os
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
        if os.path.isdir(candidate) and any(
            os.path.isdir(os.path.join(candidate, d))
            for d in os.listdir(candidate) if "chromium" in d.lower()
        ):
            return True
    # Couldn't confirm a browser install; be optimistic so we still attempt
    # extraction (the launch will raise a clear error if missing).
    return True


def get_extractor(url: str) -> BaseExtractor | None:
    """Return an extractor instance that can handle `url`, or None.

    Returns None (and logs a hint) if Playwright isn't available, so the bot
    falls back to yt-dlp. This keeps the bot usable on phones/limited envs.
    """
    if not _PLAYWRIGHT_AVAILABLE:
        _log.warning(
            "Playwright non installato: i siti streaming italiani non saranno "
            "supportati (yt-dlp diretto rimane attivo). Installa con: "
            "pip install playwright && playwright install chromium"
        )
        return None
    for cls in _EXTRACTORS:
        if cls.can_handle(url):
            return cls()
    return None


__all__ = ["BaseExtractor", "PlaywrightVideoExtractor", "VideoInfo",
           "get_extractor", "is_playwright_ready"]
