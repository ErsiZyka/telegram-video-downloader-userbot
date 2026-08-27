"""XinIndia extractor (xinindia.com).

The site serves videos through a tokenized player iframe (stream1.exiporn.com
-> CDN mp4). The crucial quirk: the player is NOT mounted when Chromium runs
headless — the page stays on the related-videos grid and no stream is ever
requested. Running Chromium with a real X display (Xvfb virtual framebuffer)
fixes it: the iframe loads and the direct CDN mp4 is intercepted normally by
PlaywrightVideoExtractor.

The direct CDN URL requires the browser request headers (Referer to the
stream1 origin), which PlaywrightVideoExtractor already captures and returns.
"""

from __future__ import annotations

import asyncio
import os
import subprocess

from bot.extractors.base import PlaywrightVideoExtractor
from bot.logging_config import get_logger

_log = get_logger("extractor")


class XinIndiaExtractor(PlaywrightVideoExtractor):
    DOMAINS: tuple[str, ...] = ("xinindia.com",)
    RENDER_WAIT = 8
    AFTER_CLICK_WAIT = 12
    HEADLESS: bool = False  # player refuses to mount headless

    # Framebuffer used when no X display is available (e.g. systemd service).
    _XVFB_DISPLAY = ":99"

    def _ensure_display(self) -> None:
        """Set DISPLAY for headed Chromium: use the existing Xvfb if present,
        otherwise start one in background (virtual, nothing visible on screen)."""
        if os.environ.get("DISPLAY"):
            return
        xvfb_sock = f"/tmp/.X11-unix/X{self._XVFB_DISPLAY[1:]}"
        if os.path.exists(xvfb_sock):
            os.environ["DISPLAY"] = self._XVFB_DISPLAY
            return
        try:
            _log.info("Avvio Xvfb %s per xinindia", self._XVFB_DISPLAY)
            subprocess.Popen(
                ["Xvfb", self._XVFB_DISPLAY, "-screen", "0", "1280x720x24"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            os.environ["DISPLAY"] = self._XVFB_DISPLAY
        except FileNotFoundError:
            _log.warning("Xvfb non trovato; provo display :0")
            os.environ["DISPLAY"] = ":0"

    async def extract(self, url: str):
        self._ensure_display()
        # Give Xvfb a moment to create the socket on first run.
        if not os.path.exists(f"/tmp/.X11-unix/X{self._XVFB_DISPLAY[1:]}"):
            await asyncio.sleep(1.5)
        return await super().extract(url)