"""Extractor registry: try each extractor, return the first that matches."""

from bot.extractors.base import BaseExtractor, PlaywrightVideoExtractor, VideoInfo
from bot.extractors.altadefinizione import AltaDefinizioneExtractor
from bot.extractors.streamingcommunity import StreamingCommunityExtractor

# Order matters: more specific first. Currently all are Playwright-based.
_EXTRACTORS: list[type[BaseExtractor]] = [
    AltaDefinizioneExtractor,
    StreamingCommunityExtractor,
]


def get_extractor(url: str) -> BaseExtractor | None:
    """Return an extractor instance that can handle `url`, or None."""
    for cls in _EXTRACTORS:
        if cls.can_handle(url):
            return cls()
    return None


__all__ = ["BaseExtractor", "PlaywrightVideoExtractor", "VideoInfo",
           "get_extractor"]
