import pytest
from bot.extractors import get_extractor, VideoInfo
from bot.extractors.base import BaseExtractor, PlaywrightVideoExtractor
from bot.extractors.hentaiworld import HentaiWorldExtractor
from bot.extractors.altadefinizione import AltaDefinizioneExtractor
from bot.extractors.streamingcommunity import StreamingCommunityExtractor


class TestCanHandle:
    def test_altadefinizione_matches(self):
        assert AltaDefinizioneExtractor.can_handle("https://altadefinizione.hot/avventura/33821-x.html")
        assert AltaDefinizioneExtractor.can_handle("https://altadefinizione.live/watch/123")

    def test_altadefinizione_no_match(self):
        assert not AltaDefinizioneExtractor.can_handle("https://youtube.com/watch?v=x")
        assert not AltaDefinizioneExtractor.can_handle("https://streamingcommunity.pizza/it/watch/60266")

    def test_streamingcommunity_matches(self):
        assert StreamingCommunityExtractor.can_handle("https://streamingcommunityz.pizza/it/watch/60266")
        assert StreamingCommunityExtractor.can_handle("https://streamingcommunity.art/it/watch/123")

    def test_streamingcommunity_no_match(self):
        assert not StreamingCommunityExtractor.can_handle("https://youtube.com/watch?v=x")
        assert not StreamingCommunityExtractor.can_handle("https://altadefinizione.hot/x")

    def test_hentaiworld_matches(self):
        assert HentaiWorldExtractor.can_handle("https://www.hentaiworld.me/watch/furachi-episode-2")
        assert HentaiWorldExtractor.can_handle("https://hentaiworld.me/watch/abc")

    def test_hentaiworld_no_match(self):
        assert not HentaiWorldExtractor.can_handle("https://youtube.com/watch?v=x")

    def test_case_insensitive(self):
        assert AltaDefinizioneExtractor.can_handle("HTTPS://Altadefinizione.HOT/x")


class TestRegistry:
    def test_get_extractor_altadefinizione(self):
        # We need to make sure playwright import check doesn't block if we mock it,
        # but since playwright is installed here it will return AltaDefinizioneExtractor.
        # HentaiWorldExtractor doesn't need Playwright, so it should always be returned.
        ext = get_extractor("https://altadefinizione.hot/x")
        if ext is not None:
            assert isinstance(ext, AltaDefinizioneExtractor)

    def test_get_extractor_streamingcommunity(self):
        ext = get_extractor("https://streamingcommunityz.pizza/it/watch/60266")
        if ext is not None:
            assert isinstance(ext, StreamingCommunityExtractor)

    def test_get_extractor_hentaiworld(self):
        ext = get_extractor("https://hentaiworld.me/watch/abc")
        assert isinstance(ext, HentaiWorldExtractor)

    def test_get_extractor_none(self):
        assert get_extractor("https://youtube.com/watch?v=x") is None
        assert get_extractor("https://it.youporn.com/watch/123") is None

    def test_get_extractor_returns_instance(self):
        ext = get_extractor("https://altadefinizione.hot/x")
        assert isinstance(ext, BaseExtractor)


class TestVideoInfo:
    def test_default_headers_empty(self):
        v = VideoInfo(url="https://x/a.m3u8", title="t")
        assert v.headers == {}

    def test_headers_set(self):
        v = VideoInfo(url="https://x/a.m3u8", title="t", headers={"Referer": "https://y/"})
        assert v.headers == {"Referer": "https://y/"}
