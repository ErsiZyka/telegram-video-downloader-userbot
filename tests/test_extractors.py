import pytest
from bot.extractors import get_extractor, VideoInfo
from bot.extractors.base import BaseExtractor, PlaywrightVideoExtractor
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

    def test_case_insensitive(self):
        assert AltaDefinizioneExtractor.can_handle("HTTPS://Altadefinizione.HOT/x")


class TestRegistry:
    def test_get_extractor_altadefinizione(self):
        ext = get_extractor("https://altadefinizione.hot/x")
        assert isinstance(ext, AltaDefinizioneExtractor)

    def test_get_extractor_streamingcommunity(self):
        ext = get_extractor("https://streamingcommunityz.pizza/it/watch/60266")
        assert isinstance(ext, StreamingCommunityExtractor)

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
