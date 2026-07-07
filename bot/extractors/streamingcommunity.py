"""Extractor for streamingcommunity.* (Cloudflare-protected, JS player)."""

from bot.extractors.base import PlaywrightVideoExtractor


class StreamingCommunityExtractor(PlaywrightVideoExtractor):
    DOMAINS = ("streamingcommunity",)
