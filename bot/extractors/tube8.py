"""Tube8 extractor: HTTP-only (no browser needed).

The official yt-dlp Tube8 extractor is marked _WORKING = False and its URL
regex expects 3 path segments (category/slug/id) while the current site uses
`/porn-video/<id>/` (2 segments). As a result yt-dlp falls back to the
Generic extractor, which grabs the wrong asset (a 0-byte SVG avatar).

This extractor implements the current flow directly:
  1. GET the page with `age_verified=1` cookie (site requires it).
  2. Extract the embedded `mediaDefinitions` JSON (signed `/media/hls/?s=...`).
  3. Call the signed HLS endpoint -> JSON with real CDN HLS URLs (t8cdn.com).
  4. Pick the highest resolution and return it with the headers the CDN needs.
"""

from __future__ import annotations

import json
import re
import urllib.request
import urllib.parse

from bot.extractors.base import BaseExtractor, VideoInfo
from bot.logging_config import get_logger

_log = get_logger("extractor")

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
       "AppleWebKit/537.36 (KHTML, like Gecko) "
       "Chrome/126.0.0.0 Safari/537.36")


class Tube8Extractor(BaseExtractor):
    DOMAINS: tuple[str, ...] = ("tube8.com", "tube8.it")

    def _fetch(self, url: str, referer: str | None = None) -> bytes:
        headers = {
            "User-Agent": _UA,
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "Cookie": "age_verified=1",
        }
        if referer:
            headers["Referer"] = referer
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=25) as r:
            return r.read()

    async def extract(self, url: str) -> VideoInfo:
        # 1. Page
        page = self._fetch(url).decode("utf-8", errors="replace")
        m = re.search(r"mediaDefinitions\"\s*:\s*(\[.*?\])\s*,\s*\"", page, re.S)
        if not m:
            raise RuntimeError("mediaDefinitions non trovati nella pagina tube8")
        defs = json.loads(m.group(1))

        hls_signed = None
        for d in defs:
            if d.get("format") == "hls" and d.get("videoUrl"):
                hls_signed = d["videoUrl"]
                break
        if not hls_signed:
            raise RuntimeError("Nessun HLS firmato in mediaDefinitions tube8")

        # 2. Signed endpoint -> real CDN URLs
        _log.info("Tube8: risolvo endpoint firmato HLS...")
        raw = self._fetch(hls_signed, referer="https://www.tube8.com/")
        try:
            media = json.loads(raw)
        except json.JSONDecodeError:
            raise RuntimeError("Risposta non JSON dall'endpoint HLS tube8")

        best = None
        for item in media:
            if item.get("format") != "hls":
                continue
            height = int(item.get("height") or 0)
            if best is None or height > best[0]:
                best = (height, item)
        if best is None:
            raise RuntimeError("Nessuna qualità HLS nella risposta tube8")

        cdn_url = best[1]["videoUrl"]
        # 3. CDN headers: the player sends Referer + UA; keep both.
        headers = {
            "User-Agent": _UA,
            "Referer": "https://www.tube8.com/",
            "Accept": "*/*",
        }
        title = self._page_title(page)
        _log.info("Tube8: HLS scelto %dp -> %s", best[0], cdn_url[:80])
        return VideoInfo(url=cdn_url, title=title, headers=headers)

    def _page_title(self, page: str) -> str:
        m = re.search(r"<title>(.*?)</title>", page, re.S | re.I)
        if not m:
            return "Video"
        title = re.sub(r"\s+", " ", m.group(1)).strip()
        # strip the site suffix: tube8 appends "Porn Videos - T8" / "- Tube8"
        # to every page title; cut at the first occurrence of the marker.
        cut = re.search(r"\b(Porn|Tube8)\b", title)
        if cut:
            title = title[: cut.start()].strip().rstrip("-|")
        return title or "Video"
