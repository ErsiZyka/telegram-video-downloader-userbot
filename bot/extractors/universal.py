"""Universal Playwright fallback extractor for free tube sites.

Uses the generic PlaywrightVideoExtractor (headless Chromium intercepts the
first HLS/mp4 stream from the page's own player, with the site handling its
own tokens/cookies transparently). No per-site code needed: adding a new site
= adding its domain to DOMAINS below.

This extractor is NOT in the main registry: it is used as a FALLBACK when
yt-dlp fails (broken/outdated extractor, Cloudflare 403, etc.). Sites with a
working yt-dlp extractor never pay the browser cost.
"""

from __future__ import annotations

from bot.extractors.base import PlaywrightVideoExtractor


class UniversalPlaywrightExtractor(PlaywrightVideoExtractor):
    DOMAINS: tuple[str, ...] = (
        "3movs.com",
        "4tube.com",
        "alphaporno.com",
        "amateurxtv.com",
        "analxtv.com",
        "anysex.com",
        "apetube.com",
        "ashemaletube.com",
        "asiantubesex.com",
        "asianxtv.com",
        "bulktube.com",
        "cliphunter.com",
        "drtuber.com",
        "empflix.com",
        "eporner.com",
        "eroxia.com",
        "extremetube.com",
        "fapvid.com",
        "femdom-tube.com",
        "flingtube.com",
        "foxhq.com",
        "freeamateurstube.com",
        "fucktube.com",
        "fuckuh.com",
        "fuq.com",
        "fux.com",
        "gifporntube.com",
        "gotporn.com",
        "hardsextube.com",
        "hclips.com",
        "hdporn.net",
        "hentaixtv.com",
        "hq69.com",
        "hqtgp.com",
        "iceporn.com",
        "jizzbox.com",
        "keezmovies.com",
        "kuntfutube.com",
        "labatidora.net",
        "lubetube.com",
        "maturetube.com",
        "maxjizztube.com",
        "motherless.com",
        "my18tube.com",
        "nuvid.com",
        "ok.xxx",
        "pervclips.com",
        "porn.com",
        "porndig.com",
        "porngo.com",
        "pornhub.com",
        "pornone.com",
        "porntrex.com",
        "pornwhite.com",
        "redtube.com",
        "spankbang.com",
        "spankingtube.com",
        "sunporno.com",
        "sxyprn.com",
        "teufelchens.tv",
        "thegootube.com",
        "thisav.com",
        "tnaflix.com",
        "tube8.com",
        "tubeon.com",
        "txxx.com",
        "vjav.com",
        "vporn.com",
        "vxxx.com",
        "xfreehd.com",
        "xgroovy.com",
        "xhamster.com",
        "xnxx.com",
        "xpee.com",
        "xvideos.com",
        "xxxaporn.com",
        "youjizz.com",
        "youporn.com",
        "yourfreeporn.us",
        "yteenporn.com",
    )
    RENDER_WAIT = 12
    AFTER_CLICK_WAIT = 15
