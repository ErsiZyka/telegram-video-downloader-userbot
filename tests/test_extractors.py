from bot.extractors import VideoInfo, get_extractor
from bot.extractors.altadefinizione import AltaDefinizioneExtractor
from bot.extractors.base import BaseExtractor
from bot.extractors.hentaiworld import HentaiWorldExtractor
from bot.extractors.internetchicks import InternetchicksExtractor, _find_embeds
from bot.extractors.porn4fans import Porn4FansExtractor, _resolve_direct_url
from bot.extractors.streamingcommunity import StreamingCommunityExtractor
from bot.extractors.surrit import SurritExtractor, _pick_m3u8
from bot.extractors.universal import (
    UniversalPlaywrightExtractor,
    XVideoTubeExtractor,
)


class TestCanHandle:
    def test_altadefinizione_matches(self):
        assert AltaDefinizioneExtractor.can_handle(
            "https://altadefinizione.hot/avventura/33821-x.html"
        )
        assert AltaDefinizioneExtractor.can_handle(
            "https://altadefinizione.live/watch/123"
        )

    def test_altadefinizione_no_match(self):
        assert not AltaDefinizioneExtractor.can_handle("https://youtube.com/watch?v=x")
        assert not AltaDefinizioneExtractor.can_handle(
            "https://streamingcommunity.pizza/it/watch/60266"
        )

    def test_streamingcommunity_matches(self):
        assert StreamingCommunityExtractor.can_handle(
            "https://streamingcommunityz.pizza/it/watch/60266"
        )
        assert StreamingCommunityExtractor.can_handle(
            "https://streamingcommunity.art/it/watch/123"
        )

    def test_streamingcommunity_no_match(self):
        assert not StreamingCommunityExtractor.can_handle(
            "https://youtube.com/watch?v=x"
        )
        assert not StreamingCommunityExtractor.can_handle(
            "https://altadefinizione.hot/x"
        )

    def test_hentaiworld_matches(self):
        assert HentaiWorldExtractor.can_handle(
            "https://www.hentaiworld.me/watch/furachi-episode-2"
        )
        assert HentaiWorldExtractor.can_handle("https://hentaiworld.me/watch/abc")

    def test_hentaiworld_no_match(self):
        assert not HentaiWorldExtractor.can_handle("https://youtube.com/watch?v=x")

    def test_porn4fans_matches(self):
        assert Porn4FansExtractor.can_handle("https://it.porn4fans.com/video/988/x/")
        assert Porn4FansExtractor.can_handle("https://porn4fans.com/video/988/x/")
        assert Porn4FansExtractor.can_handle("HTTPS://EN.PORN4FANS.COM/x")
        assert Porn4FansExtractor.can_handle(
            "https://shareanynudes.com/video/tanababyxo-x/"
        )
        assert Porn4FansExtractor.can_handle(
            "https://x-video.tube/video/806884/mira-love-aka-mrsmiralove/"
        )

    def test_porn4fans_no_match(self):
        assert not Porn4FansExtractor.can_handle("https://youtube.com/watch?v=x")
        assert not Porn4FansExtractor.can_handle("https://missav.ws/en/x")

    def test_surrit_matches(self):
        assert SurritExtractor.can_handle("https://missav.ws/en/goin-004")
        assert SurritExtractor.can_handle("https://123av.org/dm32/en/rb049")

    def test_surrit_no_match(self):
        assert not SurritExtractor.can_handle("https://youtube.com/watch?v=x")
        assert not SurritExtractor.can_handle("https://porn4fans.com/video/1/x/")

    def test_internetchicks_matches(self):
        assert InternetchicksExtractor.can_handle(
            "https://internetchicks.com/gattouz0-x/")

    def test_internetchicks_no_match(self):
        assert not InternetchicksExtractor.can_handle("https://youtube.com/watch?v=x")

    def test_xvideo_tube_matches(self):
        # x-video.tube: yt-dlp non riconosce il player (flashvars). Ha un
        # extractor browser dedicato (XVideoTubeExtractor) che ignora il
        # pre-roll pubblicitario /roomad/ e cattura il video vero (bkcdn).
        assert XVideoTubeExtractor.can_handle(
            "https://x-video.tube/video/806884/mira-love-aka-mrsmiralove-01-06-2025-onlyfans-video-un-piccolo-assaggio-di-come-si-twerka-su-un-cazzo/"
        )
        assert XVideoTubeExtractor.can_handle("https://x-video.tube/video/1/x/")
        # Il generico NON deve più accettare il dominio: c'è l'extractor dedicato.
        assert not UniversalPlaywrightExtractor.can_handle(
            "https://x-video.tube/video/1/x/")

    def test_universal_no_match(self):
        assert not UniversalPlaywrightExtractor.can_handle("https://youtube.com/watch?v=x")
        # NB: porn4fans/123av/missav sono VOLUTAMENTE nella lista universale
        # (rete di sicurezza browser): uso un dominio estraneo per il no-match.
        assert not UniversalPlaywrightExtractor.can_handle(
            "https://example.com/video/1/x/")


    def test_find_embeds_prefers_streamtape(self):
        html = (
            "<button onclick=\"playEmbed('https://voe.sx/e/aa');\">P1</button>"
            "<button onclick=\"playEmbed('https://streamtape.com/e/bb');\">P2</button>"
            "<button onclick=\"playEmbed('https://voe.sx/e/aa');\">P3</button>"
        )
        embeds = _find_embeds(html)
        assert embeds == ["https://streamtape.com/e/bb", "https://voe.sx/e/aa"]

    def test_find_embeds_none(self):
        assert _find_embeds("<html><body>ciao</body></html>") == []

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

    def test_get_extractor_xvideo_tube(self):
        # La via principale per x-video.tube e' HTTP (porn4fans get_file):
        # il browser XVideoTubeExtractor resta solo come fallback universale.
        ext = get_extractor(
            "https://x-video.tube/video/806884/mira-love-aka-mrsmiralove/"
        )
        assert isinstance(ext, Porn4FansExtractor)

    def test_get_extractor_none(self):
        assert get_extractor("https://youtube.com/watch?v=x") is None
        assert get_extractor("https://it.youporn.com/watch/123") is None

    def test_universal_fallback_xvideo_tube_none(self):
        # x-video.tube: il fallback browser NON deve mai partire (intercetta
        # ad/gif). Via unica = extractor HTTP dedicato.
        from bot.extractors import get_universal_fallback

        fb = get_universal_fallback(
            "https://x-video.tube/video/806884/mira-love-aka-mrsmiralove-01-06-2025/"
        )
        assert fb is None


    def test_get_extractor_returns_instance(self):
        ext = get_extractor("https://altadefinizione.hot/x")
        assert isinstance(ext, BaseExtractor)


class TestVideoInfo:
    def test_default_headers_empty(self):
        v = VideoInfo(url="https://x/a.m3u8", title="t")
        assert v.headers == {}

    def test_headers_set(self):
        v = VideoInfo(
            url="https://x/a.m3u8", title="t", headers={"Referer": "https://y/"}
        )
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


class TestResolveDirectUrl:
    """Il gateway di shareanynudes è un .php (yt-dlp lo rifiuta per sicurezza):
    l'extractor deve riscriverlo in un URL mp4 diretto con gli stessi parametri.
    """

    _GATEWAY = (
        "https://sn1.nudes365.com/remote_control.php?time=1788002650"
        "&cv=84c598cbec8627bcd504c1bd33e333ae&lr=0"
        "&cv2=1536a3837146c631be79e0f8591fdd1f"
        "&file=%2Fvideos%2F4000%2F4084%2F4084_720p.mp4"
        "&cv3=8a1256d27bb6bc95f187ddd6544f12f8"
        "&cv4=17c077dd0c88349fa49752f5ed78fa7f"
    )

    @staticmethod
    def _fake_response(final_url: str, ok: bool = True):
        class _Resp:
            status = 206 if ok else 404

            headers = {"Content-Type": "video/mp4" if ok else "text/html"}

            def geturl(self):
                return final_url

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        return _Resp()

    def test_gateway_php_riscritto_in_mp4_diretto(self):
        from unittest.mock import patch

        with patch(
            "urllib.request.urlopen",
            return_value=self._fake_response(self._GATEWAY),
        ):
            result = _resolve_direct_url(
                "https://shareanynudes.com/get_file/3/x/4000/4084/4084_720p.mp4/?v-acctoken=t",
                "https://shareanynudes.com/",
            )
        assert result.startswith(
            "https://sn1.nudes365.com/videos/4000/4084/4084_720p.mp4?"
        )
        assert "time=1788002650" in result
        assert "file=" not in result
        assert "cv3=8a1256d27bb6bc95f187ddd6544f12f8" in result

    def test_url_gia_diretto_invariato(self):
        from unittest.mock import patch

        direct = "https://it.porn4fans.com/get_file/1/x/0/988/988.mp4/?v-acctoken=t"
        with patch(
            "urllib.request.urlopen",
            return_value=self._fake_response(direct),
        ):
            result = _resolve_direct_url(direct, "https://it.porn4fans.com/")
        assert result == direct

    def test_errore_rete_torna_url_originale(self):
        from unittest.mock import patch

        with patch("urllib.request.urlopen", side_effect=OSError):
            result = _resolve_direct_url(
                "https://shareanynudes.com/get_file/3/x/4000/4084/a.mp4/?v-acctoken=t",
                "https://shareanynudes.com/",
            )
        assert "remote_control.php" not in result
        assert "get_file" in result

    def test_direct_non_servito_resta_gateway(self):
        # x-video.tube: il path .mp4 riscritto NON esiste (404) ma il gateway
        # remote_control.php serve il video. Se la probe del diretto fallisce
        # si mantiene il gateway: yt-dlp lo segue e rileva il content-type.
        from unittest.mock import patch

        gateway = self._GATEWAY
        with patch(
            "urllib.request.urlopen",
            side_effect=[self._fake_response(gateway), OSError],
        ):
            result = _resolve_direct_url(
                "https://x-video.tube/get_file/4/x/812000/812722/812722_720p.mp4/?v-acctoken=t",
                "https://x-video.tube/",
            )
        assert result == gateway

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
