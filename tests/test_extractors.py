from bot.extractors import get_extractor, VideoInfo
from bot.extractors.altadefinizione import AltaDefinizioneExtractor
from bot.extractors.base import BaseExtractor
from bot.extractors.hentaiworld import HentaiWorldExtractor
from bot.extractors.porn4fans import Porn4FansExtractor
from bot.extractors.streamingcommunity import StreamingCommunityExtractor
from bot.extractors.surrit import SurritExtractor, _decode_packers, _pick_m3u8


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

    def test_porn4fans_matches(self):
        assert Porn4FansExtractor.can_handle("https://it.porn4fans.com/video/988/x/")
        assert Porn4FansExtractor.can_handle("https://porn4fans.com/video/988/x/")
        assert Porn4FansExtractor.can_handle("HTTPS://EN.PORN4FANS.COM/x")

    def test_porn4fans_no_match(self):
        assert not Porn4FansExtractor.can_handle("https://youtube.com/watch?v=x")
        assert not Porn4FansExtractor.can_handle("https://missav.ws/en/x")

    def test_surrit_matches(self):
        assert SurritExtractor.can_handle("https://missav.ws/en/goin-004")
        assert SurritExtractor.can_handle("https://123av.org/dm32/en/rb049")

    def test_surrit_no_match(self):
        assert not SurritExtractor.can_handle("https://youtube.com/watch?v=x")
        assert not SurritExtractor.can_handle("https://porn4fans.com/video/1/x/")

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

    def test_get_extractor_porn4fans(self):
        ext = get_extractor("https://it.porn4fans.com/video/12/x/")
        assert isinstance(ext, Porn4FansExtractor)

    def test_get_extractor_surrit(self):
        ext = get_extractor("https://missav.ws/en/x")
        assert isinstance(ext, SurritExtractor)
        ext2 = get_extractor("https://123av.org/dm32/en/x")
        assert isinstance(ext2, SurritExtractor)

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


# Packer di Dean Edwards esattamente come lo serve missav.ws (vedi pagina
# reale: i token numerici base16 mappano nel dizionario superando l'offuscamento).
_PACKER_FIXTURE = (
    "<html><script>"
    "eval(function(p,a,c,k,e,d){e=function(c){return c.toString(36)};"
    "if(!''.replace(/^/,String)){while(c--){d[c.toString(a)]=k[c]||c.toString(a)}"
    "k=[function(e){return d[e]}];e=function(){return'\\w+'};c=1};"
    "while(c--){if(k[c]){p=p.replace(new RegExp('\\b'+e(c)+'\\b','g'),k[c])}}"
    "return p}('f=\\'8://7.6/5-4-3-2-1/e.0\\';"
    "d=\\'8://7.6/5-4-3-2-1/c/9.0\\';"
    "b=\\'8://7.6/5-4-3-2-1/a/9.0\\';',16,16,"
    "'m3u8|cd0dfdf265cd|9e3b|4c84|e3b2|ea1d04f4|com|surrit|https|video|"
    "1080p|source1280|720p|source842|playlist|source'.split('|'),0,{}))"
    "</script></html>"
)


class TestSurritPacker:
    def test_decode_packers_extracts_m3u8(self):
        decoded = _decode_packers(_PACKER_FIXTURE)
        assert len(decoded) == 1
        body = decoded[0]
        assert "https://surrit.com/ea1d04f4-e3b2-4c84-9e3b-cd0dfdf265cd/playlist.m3u8" in body
        assert "1080p/video.m3u8" in body
        assert "720p/video.m3u8" in body

    def test_pick_m3u8_prefers_master_playlist(self):
        urls = [
            "https://surrit.com/uuid/1080p/video.m3u8",
            "https://surrit.com/uuid/720p/video.m3u8",
            "https://surrit.com/uuid/playlist.m3u8",
            "https://edge-hls.growcdnssedge.com/ad/1.m3u8",  # AD da ignorare
        ]
        assert _pick_m3u8(urls) == "https://surrit.com/uuid/playlist.m3u8"

    def test_pick_m3u8_ignores_ads_and_picks_best_media(self):
        urls = [
            "https://surrit.com/uuid/720p/video.m3u8",
            "https://surrit.com/uuid/1080p/video.m3u8",
            "https://edge-hls.growcdnssedge.com/ad/1.m3u8",
        ]
        assert _pick_m3u8(urls) == "https://surrit.com/uuid/1080p/video.m3u8"

    def test_pick_m3u8_none_when_only_ads(self):
        assert _pick_m3u8(["https://edge-hls.growcdnssedge.com/ad/1.m3u8"]) is None
