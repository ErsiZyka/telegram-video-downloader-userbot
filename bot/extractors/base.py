"""Base extractor interface + PlaywrightVideoExtractor (headless Chromium)."""

from __future__ import annotations

import asyncio
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from playwright.async_api import async_playwright, Error as PlaywrightError

from bot.logging_config import get_logger

_log = get_logger("extractor")


@dataclass
class VideoInfo:
    """Result of extraction: a direct video URL (m3u8/mp4) + title, plus the
    HTTP headers the CDN requires to serve that URL (anti-leech protection:
    streaming-community/vidxgo CDNs check sec-fetch-* and client hints)."""
    url: str
    title: str
    headers: dict = field(default_factory=dict)


class BaseExtractor(ABC):
    """Abstract extractor. Each site = one subclass with DOMAINS set."""
    DOMAINS: tuple[str, ...] = ()

    @classmethod
    def can_handle(cls, url: str) -> bool:
        url_lower = url.lower()
        return any(dom in url_lower for dom in cls.DOMAINS)

    @abstractmethod
    async def extract(self, url: str) -> VideoInfo:
        ...


def _is_stream_response(url: str, content_type: str) -> bool:
    """A response is 'the video' if it's an HLS playlist (by content-type or
    .m3u8 in the URL) or a direct mp4. The streaming-community/vidxgo CDN serves
    m3u8 with content-type 'application/vnd.apple.mpegurl' and the URL carries a
    query string, so we must match by content-type, not extension."""
    ct = (content_type or "").lower()
    u = (url or "").lower()
    if "mpegurl" in ct or "x-mpegurl" in ct:
        return True
    if ".m3u8" in u:
        return True
    if ".mp4" in u and "youtube" not in u and "googlevideo" not in u:
        return True
    return False


# Headers that streaming-community / vidxgo CDNs require to serve the m3u8.
# The CDN validates sec-fetch-* and client hints; without them it returns 403
# to yt-dlp/ffmpeg/curl. These mirror what a real Chrome browser sends.
_STREAM_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/120.0.0.0 Safari/537.36"),
    "Referer": "https://v.vidxgo.co/",
    "sec-ch-ua": '"HeadlessChrome";v="149", "Chromium";v="149", '
                 '"Not)A;Brand";v="24"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "accept-language": "it-IT",
    "sec-fetch-site": "cross-site",
    "sec-fetch-mode": "cors",
    "sec-fetch-dest": "empty",
}


class PlaywrightVideoExtractor(BaseExtractor):
    """Generic Cloudflare-safe extractor: launch headless Chromium, navigate to
    the page, wait for the JS player to render, click play, and intercept the
    first HLS (.m3u8) or direct mp4 response from network traffic — including
    inside cross-origin iframes.

    The m3u8 token from these CDNs is one-time/session-bound, so the extractor
    aborts the browser's own fetch of the master playlist (leaving the token
    unconsumed) and returns the URL + required headers for yt-dlp to download.

    Works for any site whose player loads an HLS/MP4 stream (streamingcommunity,
    altadefinizione, vidxgo, etc.). To support a new site, subclass and set
    DOMAINS — no other code needed.
    """

    DOMAINS: tuple[str, ...] = ()
    RENDER_WAIT = 10   # seconds to wait for the JS player to render
    AFTER_CLICK_WAIT = 12  # seconds to wait after clicking play for the stream

    async def extract(self, url: str) -> VideoInfo:
        video_url: str | None = None
        future: asyncio.Future[str] = asyncio.get_running_loop().create_future()

        async def _on_response(response):
            nonlocal video_url
            if video_url is not None or future.done():
                return
            try:
                u = response.url
                ct = response.headers.get("content-type", "")
            except Exception:
                return
            if _is_stream_response(u, ct):
                video_url = u
                _log.info("Stream intercettato: %s", u[:100])
                if not future.done():
                    future.set_result(u)

        try:
            async with async_playwright() as p:
                _log.info("Avvio Chromium headless per %s", url)
                browser = await p.chromium.launch(
                    headless=True,
                    args=["--disable-blink-features=AutomationControlled"],
                )
                context = await browser.new_context(
                    user_agent=_STREAM_HEADERS["User-Agent"],
                    locale="it-IT",
                    viewport={"width": 1280, "height": 720},
                )
                # Hide the webdriver flag so anti-bot JS doesn't detect headless.
                await context.add_init_script(
                    "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
                )
                page = await context.new_page()
                # Attach the response listener to the page and to every frame:
                # streaming players (vidxgo) load the m3u8 inside a cross-origin
                # iframe whose responses the top-level page cannot see.
                page.on("response", _on_response)
                page.on(
                    "frameattached",
                    lambda f: f.on("response", _on_response),
                )
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=20000)
                    _log.info("Pagina caricata: %s", url)
                except PlaywrightError as e:
                    _log.warning("goto timeout/errore (procedo): %s", e)
                for fr in page.frames:
                    fr.on("response", _on_response)

                try:
                    title = (await page.title()) or "Video"
                except Exception:
                    title = "Video"

                # Wait for the JS player to render, then click play inside the
                # player frame. Use JS evaluate() to click (bypasses the
                # 'element is outside the viewport' check that breaks headless).
                if video_url is None:
                    _log.info("Attesa render player (%ds)...", self.RENDER_WAIT)
                    await asyncio.sleep(self.RENDER_WAIT)
                    _log.info("Click play button...")
                    await self._click_play(page)
                    try:
                        await asyncio.wait_for(future, timeout=self.AFTER_CLICK_WAIT)
                    except asyncio.TimeoutError:
                        _log.warning("Nessuno stream entro %ds dopo il click", self.AFTER_CLICK_WAIT)

                await context.close()
                await browser.close()
        except PlaywrightError as e:
            _log.error("Browser error: %s", e)
            raise RuntimeError(f"Browser error: {e}") from e

        if video_url is None:
            raise RuntimeError("Nessuno stream video trovato entro il timeout.")
        _log.info("Stream trovato: %s", video_url[:100])
        return VideoInfo(url=video_url, title=title, headers=dict(_STREAM_HEADERS))

    async def _click_play(self, page) -> None:
        """Find the player (possibly inside an iframe) and click its play button
        via JS evaluate, which works even when the element is off-screen."""
        # Prefer the player frame (vidxgo/streamingcommunity), fall back to top.
        targets = []
        for fr in page.frames:
            if fr != page.main_frame:
                targets.append(fr)
        targets.append(page.main_frame)
        for frame in targets:
            try:
                if not frame.url:
                    continue
            except Exception:
                continue
            for selector in (".btn-play", ".play-pause-center",
                             ".vjs-big-play-button", ".jw-display-icon-display",
                             ".plyr__control--overlaid", "video"):
                try:
                    clicked = await frame.evaluate(
                        """(sel) => {
                            const el = document.querySelector(sel);
                            if (!el) return false;
                            el.click();
                            return true;
                        }""",
                        selector,
                    )
                    if clicked:
                        return
                except Exception:
                    continue
