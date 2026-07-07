# Extractor Modulare (Playwright) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Sistema modulare di extractor basati su Playwright per scaricare video da siti streaming italiani protetti da Cloudflare (altadefinizione, streamingcommunity) non supportati da yt-dlp.

**Architecture:** `bot/extractors/` con `BaseExtractor` (ABC) + `PlaywrightVideoExtractor` (lancia Chromium headless, intercetta m3u8/mp4 dal traffico di rete). Sottoclassi da 2 righe per ogni sito. Integrazione in `_process_link` prima del flusso yt-dlp.

**Tech Stack:** Python 3.12, Playwright 1.61 (già installato + Chromium), yt-dlp (scarica m3u8 generici).

**Spec:** `docs/superpowers/specs/2026-07-07-extractors-design.md`

## Global Constraints

- Playwright API async (`playwright.async_api`)
- Un browser nuovo per estrazione (no stato condiviso)
- Timeout 30s per intercettare lo stream
- `can_handle` matcha per dominio (substring, case-insensitive) — gestisce domini che cambiano (.hot/.pizza/etc.)
- L'URL m3u8 estratto va passato a yt-dlp via `download_video` (invariato)
- I 76 test esistenti devono continuare a passare

---

## Task 1: base.py + __init__.py (registro e PlaywrightVideoExtractor)

**Files:**
- Create: `bot/extractors/__init__.py`
- Create: `bot/extractors/base.py`

- [ ] **Step 1: Create base.py**

```python
"""Base extractor interface + PlaywrightVideoExtractor (headless Chromium)."""

from __future__ import annotations

import asyncio
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass

from playwright.async_api import async_playwright, Error as PlaywrightError


@dataclass
class VideoInfo:
    """Result of extraction: a direct video URL (m3u8/mp4) + title."""
    url: str
    title: str


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


class PlaywrightVideoExtractor(BaseExtractor):
    """Generic Cloudflare-safe extractor: launch headless Chromium, navigate to
    the page, intercept the first .m3u8 or .mp4 response from network traffic.

    Works for any site whose player loads an HLS/MP4 stream. To support a new
    site, subclass and set DOMAINS — no other code needed.
    """

    DOMAINS: tuple[str, ...] = ()
    # How long to wait for the stream URL to appear (seconds).
    STREAM_TIMEOUT = 30
    # Regex for URLs we consider "the video". m3u8 (HLS master/variant) first,
    # then direct mp4.
    _STREAM_RE = re.compile(r"\.(?:m3u8|mp4)(?:\?|#|$)", re.IGNORECASE)

    async def extract(self, url: str) -> VideoInfo:
        video_url: str | None = None
        future: asyncio.Future[str] = asyncio.get_running_loop().create_future()

        async def _on_response(response):
            nonlocal video_url
            if video_url is not None or future.done():
                return
            try:
                u = response.url
            except Exception:
                return
            if self._STREAM_RE.search(u):
                video_url = u
                if not future.done():
                    future.set_result(u)

        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                context = await browser.new_context(
                    user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                "AppleWebKit/537.36 (KHTML, like Gecko) "
                                "Chrome/120.0.0.0 Safari/537.36"),
                    locale="it-IT",
                )
                page = await context.new_page()
                page.on("response", _on_response)
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=20000)
                except PlaywrightError:
                    pass  # navigation may timeout but stream can still be caught
                try:
                    title = (await page.title()) or "Video"
                except Exception:
                    title = "Video"
                # Try clicking a play button if present (streamingcommunity, etc.)
                if video_url is None:
                    for selector in ("button.play", ".play", ".vjs-big-play-button",
                                     "[class*='play']", "video"):
                        try:
                            el = await page.query_selector(selector)
                            if el is not None:
                                await el.click(timeout=2000)
                                break
                        except Exception:
                            continue
                # Wait for the stream URL to be captured
                try:
                    await asyncio.wait_for(future, timeout=self.STREAM_TIMEOUT)
                except asyncio.TimeoutError:
                    pass
                await context.close()
                await browser.close()
        except PlaywrightError as e:
            raise RuntimeError(f"Browser error: {e}") from e

        if video_url is None:
            raise RuntimeError("Nessuno stream video trovato entro il timeout.")
        return VideoInfo(url=video_url, title=title)
```

- [ ] **Step 2: Create __init__.py with registry**

```python
"""Extractor registry: try each extractor, return the first that matches."""

from bot.extractors.base import BaseExtractor, PlaywrightVideoExtractor, VideoInfo
from bot.extractors.altadefinizione import AltaDefinizioneExtractor
from bot.extractors.streamingcommunity import StreamingCommunityExtractor

# Order matters: more specific first. Currently all are Playwright-based.
_EXTRACTORS: list[type[BaseExtractor]] = [
    AltaDefinizioneExtractor,
    StreamingCommunityExtractor,
]


def get_extractor(url: str) -> BaseExtractor | None:
    """Return an extractor instance that can handle `url`, or None."""
    for cls in _EXTRACTORS:
        if cls.can_handle(url):
            return cls()
    return None


__all__ = ["BaseExtractor", "PlaywrightVideoExtractor", "VideoInfo",
           "get_extractor"]
```

- [ ] **Step 3: Commit Task 1 (will fail import until Task 2 creates the subclasses)**

Skip commit — Task 2 immediately follows. Do Task 2 before testing imports.

---

## Task 2: Concrete extractors (altadefinizione + streamingcommunity)

**Files:**
- Create: `bot/extractors/altadefinizione.py`
- Create: `bot/extractors/streamingcommunity.py`

- [ ] **Step 1: Create altadefinizione.py**

```python
"""Extractor for altadefinizione.* (Cloudflare-protected, JS player)."""

from bot.extractors.base import PlaywrightVideoExtractor


class AltaDefinizioneExtractor(PlaywrightVideoExtractor):
    DOMAINS = ("altadefinizione.",)
```

- [ ] **Step 2: Create streamingcommunity.py**

```python
"""Extractor for streamingcommunity.* (Cloudflare-protected, JS player)."""

from bot.extractors.base import PlaywrightVideoExtractor


class StreamingCommunityExtractor(PlaywrightVideoExtractor):
    DOMAINS = ("streamingcommunity",)
```

- [ ] **Step 3: Verify imports work**

Run:
```bash
cd /c/Users/ersi/video_downloader_bot
C:/Users/ersi/AppData/Local/Programs/Python/Python312/python.exe -c "import sys; sys.path.insert(0,'.'); from bot.extractors import get_extractor, VideoInfo; print('IMPORT OK')"
```
Expected: `IMPORT OK`

- [ ] **Step 4: Commit**

```bash
git add bot/extractors/
git commit -m "feat: modular extractor system (Playwright) + altadefinizione + streamingcommunity"
```

---

## Task 3: Tests for extractors

**Files:**
- Create: `tests/test_extractors.py`

- [ ] **Step 1: Write tests (can_handle + registry, no real browser)**

```python
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
```

- [ ] **Step 2: Run tests**

Run: `C:/Users/ersi/AppData/Local/Programs/Python/Python312/python.exe -m pytest tests/test_extractors.py -v`
Expected: PASS (7 tests)

- [ ] **Step 3: Run full suite**

Run: `C:/Users/ersi/AppData/Local/Programs/Python/Python312/python.exe -m pytest tests/ -q`
Expected: PASS (83 tests = 76 + 7)

- [ ] **Step 4: Commit**

```bash
git add tests/test_extractors.py
git commit -m "test: extractor can_handle + registry"
```

---

## Task 4: Integration in _process_link

**Files:**
- Modify: `bot/handlers.py` — `_process_link`

- [ ] **Step 1: Add import + extractor check at top of _process_link**

Add import near the top of `bot/handlers.py` (with other bot imports):
```python
from bot.extractors import get_extractor
```

In `_process_link`, right at the start of the function (before the history check), add the extractor branch. Read the current start of `_process_link` first, then edit.

Current start:
```python
async def _process_link(client, event, url: str, channel_id: int) -> None:
    user_id = event.sender_id

    entry = _get_history().get(url)
```

Replace with:
```python
async def _process_link(client, event, url: str, channel_id: int) -> None:
    user_id = event.sender_id

    # Streaming-site extractors (Playwright) take priority over yt-dlp for
    # Cloudflare-protected sites yt-dlp can't handle.
    extractor = get_extractor(url)
    if extractor is not None:
        status_msg = await _safe_reply(event, "🌐 Estrazione video via browser...")
        try:
            loop = asyncio.get_running_loop()
            info = await extractor.extract(url)
            _log(f"Extractor ok: {info.url}")
            await _safe_delete(status_msg)
            await _send_quality_menu(event, user_id, info.url, info.title)
        except Exception as e:
            _log(f"Extractor fallito: {e!r}")
            await _safe_edit(status_msg, f"❌ Estrazione fallita: {e}")
        return

    entry = _get_history().get(url)
```

- [ ] **Step 2: Run full test suite**

Run: `C:/Users/ersi/AppData/Local/Programs/Python/Python312/python.exe -m pytest tests/ -q`
Expected: PASS (83 tests)

- [ ] **Step 3: Commit**

```bash
git add bot/handlers.py
git commit -m "feat: integrate extractors in _process_link (Playwright before yt-dlp)"
```

---

## Task 5: requirements.txt + manual smoke test

**Files:**
- Modify: `requirements.txt`

- [ ] **Step 1: Add playwright to requirements.txt**

Append to `requirements.txt`:
```
playwright>=1.40.0
```

- [ ] **Step 2: Run full test suite**

Run: `C:/Users/ersi/AppData/Local/Programs/Python/Python312/python.exe -m pytest tests/ -q`
Expected: PASS (83 tests)

- [ ] **Step 3: Smoke test — bot boots**

Run (background):
```bash
cd /c/Users/ersi/video_downloader_bot
rm -f /tmp/bot.log
PYTHONIOENCODING=utf-8 C:/Users/ersi/AppData/Local/Programs/Python/Python312/python.exe -u run.py > /tmp/bot.log 2>&1 &
sleep 22
cat /tmp/bot.log
kill %1 2>/dev/null
```
Expected: "Userbot avviato e in ascolto", "Queue worker avviato", no traceback.

- [ ] **Step 4: Commit + push**

```bash
git add requirements.txt
git commit -m "chore: add playwright to requirements.txt"
git push
```
