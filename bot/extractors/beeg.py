"""Beeg extractor via Playwright.

The official yt-dlp Beeg extractor is broken: the site is now a Vue SPA that
requires a JWT Bearer token for its video API (store.externulls.com), and the
extractor passes the video id verbatim from the URL — the current API rejects
ids that carry a leading zero (beeg.com/-0<id>), returning HTTP 400.

Instead of re-implementing the token flow (fragile, changes often), we use the
generic PlaywrightVideoExtractor: headless Chromium loads the page, the site
itself obtains its JWT and starts the HLS stream, and we intercept the first
m3u8 response from the network — token handled transparently by the site.
"""

from __future__ import annotations

from bot.extractors.base import PlaywrightVideoExtractor


class BeegExtractor(PlaywrightVideoExtractor):
    DOMAINS: tuple[str, ...] = ("beeg.com",)
    RENDER_WAIT = 12       # the SPA needs a moment to mount and fetch the JWT
    AFTER_CLICK_WAIT = 15
