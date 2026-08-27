"""Dedicated supjav.com extractor.

supjav streams its videos as a Cloudflare-protected HLS "live relay" on
growcdnssedge.com with the proprietary MOUFLON anti-hotlink scheme
(#EXT-X-MOUFLON:URI lines carry the ONLY working segment URLs; the plain
segment lines are 404 placeholders). yt-dlp cannot parse it.

The page also loads several AD streams (storagexhd 300x250 banner mp4 +
mayzaent.com widget videos). This extractor ignores those and captures the
m3u8 (master if possible) served from growcdnssedge.com inside the MAIN
frame.

Returns the m3u8 URL + the headers (Referer/UA) required by the CDN.
The download side uses bot.downloader._download_mouflon_hls().
"""

from __future__ import annotations

import asyncio
import re

from playwright.async_api import async_playwright, Error as PlaywrightError

from bot.extractors.base import (
    PlaywrightVideoExtractor,
    VideoInfo,
    _is_stream_response,
)
from bot.logging_config import get_logger

_log = get_logger("supjav")

# Ad stream markers: skip any response that looks like an ad.
_AD_KEYWORDS = (
    "storagexhd",
    "300x250",
    "medium.mp4",
    "mayzaent",
    "trackwilltrk",
    "eix304",
    "smartpop",
    "mnaspm",
)
_AD_FRAME_KEYWORDS = (
    "mayzaent",
    "storagexhd",
    "eix304",
    "trackwilltrk",
    "mnaspm",
)

_MASTER_RE = re.compile(r"/master/", re.IGNORECASE)


class SupjavExtractor(PlaywrightVideoExtractor):
    DOMAINS: tuple[str, ...] = ("supjav.com",)
    RENDER_WAIT = 12
    AFTER_CLICK_WAIT = 15

    def _from_ad(self, url: str, frame_url: str) -> bool:
        low = url.lower()
        if any(k in low for k in _AD_KEYWORDS):
            return True
        if any(k in (frame_url or "").lower() for k in _AD_FRAME_KEYWORDS):
            return True
        return False

    async def extract(self, url: str) -> VideoInfo:
        video_url: str | None = None
        video_headers: dict = {}
        title = "Video"
        # prefer an edge-hls master (quality variants), else any growncdn m3u8
        master_candidate: str | None = None
        media_candidate: str | None = None
        ad_hits = 0

        def _on_response(response):
            nonlocal video_url, video_headers, master_candidate, media_candidate, ad_hits
            # non finalizzare finché non abbiamo un MASTER: se arriva prima una
            # media playlist, teniamola come fallback ma continuiamo ad ascoltare
            if video_url is not None:
                return
            try:
                u = response.url
                ct = response.headers.get("content-type", "")
                st = response.status
                frm = response.frame.url if response.frame else ""
            except Exception:
                return
            if st >= 400:
                return
            try:
                if self._from_ad(u, frm):
                    ad_hits += 1
                    return
            except Exception:
                pass
            if not _is_stream_response(u, ct):
                return
            if ".m3u8" not in u.lower() and "mpegurl" not in ct:
                # direct mp4 (rare on supjav) — keep as fallback
                if video_url is None:
                    video_url = u
                return
            # HLS: prefer master from edge-hls
            if "edge-hls.growcdnssedge.com" in u or _MASTER_RE.search(u):
                if master_candidate is None:
                    master_candidate = u
                    _log.info("Master m3u8: %s", u[:110])
            else:
                if media_candidate is None:
                    media_candidate = u
                    _log.info("Media m3u8: %s", u[:110])
            # finalizza SOLO col master; la media playlist resta come fallback
            if master_candidate is not None:
                video_url = master_candidate
                video_headers = {"Referer": url, "User-Agent": _STREAM_UA}

        try:
            async with async_playwright() as p:
                _log.info("Avvio Chromium per supjav: %s", url)
                browser = await p.chromium.launch(
                    headless=self.HEADLESS,
                    args=["--disable-blink-features=AutomationControlled"],
                )
                context = await browser.new_context(
                    user_agent=_STREAM_UA,
                    locale="it-IT",
                    viewport={"width": 1280, "height": 720},
                )
                await context.add_init_script(
                    "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
                )
                page = await context.new_page()
                page.on("response", _on_response)
                page.on("frameattached", lambda f: f.on("response", _on_response))
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                except PlaywrightError as e:
                    _log.warning("goto supjav warn: %s", e)
                for fr in page.frames:
                    fr.on("response", _on_response)
                try:
                    title = (await page.title()) or "Video"
                except Exception:
                    pass
                _log.info("Titolo supjav: %s", title[:80])

                # attende il player e clicca play (i relay partono al click/autoplay)
                await asyncio.sleep(self.RENDER_WAIT)
                if video_url is None:
                    await self._click_play(page)
                    await asyncio.sleep(self.AFTER_CLICK_WAIT)

                await context.close()
                await browser.close()
        except PlaywrightError as e:
            _log.error("Browser supjav error: %s", e)
            raise RuntimeError(f"Browser error: {e}") from e

        if video_url is None:
            raise RuntimeError(
                "Nessuno stream supjav trovato (ad intercettati: %d, "
                "master=%s media=%s)" % (ad_hits, bool(master_candidate), bool(media_candidate))
            )
        _log.info("Stream supjav OK: %s", video_url[:110])
        return VideoInfo(url=video_url, title=title, headers=video_headers)


_STREAM_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)