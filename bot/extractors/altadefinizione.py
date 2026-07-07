"""Extractor for altadefinizione.* (Cloudflare-protected, JS player)."""

from bot.extractors.base import PlaywrightVideoExtractor


class AltaDefinizioneExtractor(PlaywrightVideoExtractor):
    DOMAINS = ("altadefinizione",)
