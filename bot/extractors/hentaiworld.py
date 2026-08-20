"""HentaiWorld video extractor."""

from __future__ import annotations

import asyncio
import re
import urllib.request
from bot.extractors.base import BaseExtractor, VideoInfo

class HentaiWorldExtractor(BaseExtractor):
    """Extractor for hentaiworld.me.
    
    Extracts the direct mp4/m3u8 URL from the page HTML javascript variables
    without needing a browser session.
    """
    DOMAINS = ("hentaiworld.me",)

    async def extract(self, url: str) -> VideoInfo:
        loop = asyncio.get_running_loop()
        
        def _fetch():
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                    "Referer": "https://www.google.com/"
                }
            )
            with urllib.request.urlopen(req, timeout=15) as response:
                return response.read().decode("utf-8", errors="replace")
                
        html = await loop.run_in_executor(None, _fetch)
        
        # Find the videoUrl Javascript variable
        match_url = re.search(r"const\s+videoUrl\s*=\s*['\"]([^'\"]+)['\"]", html)
        if not match_url:
            match_url = re.search(r"videoUrl\s*=\s*['\"]([^'\"]+)['\"]", html)
            
        if not match_url:
            raise Exception("Impossibile trovare il link del video nella pagina di HentaiWorld.")
            
        video_url = match_url.group(1)
        
        # Extract title from <title> tag
        match_title = re.search(r"<title>([^<]+)</title>", html)
        title = match_title.group(1).strip() if match_title else "HentaiWorld Video"
        
        # Clean title
        for suffix in (" - HentaiWorld", " | HentaiWorld", " - Hentai World"):
            if title.lower().endswith(suffix.lower()):
                title = title[:-len(suffix)].strip()
                
        return VideoInfo(
            url=video_url,
            title=title,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Referer": url
            }
        )
