"""Extractor per i siti a piattaforma Surrit (missav.ws, 123av.org, ...).

Questi siti condividono lo stesso motore: la pagina del video contiene gli
URL HLS come variabili JS offuscate con il packer di Dean Edwards, es.:

    eval(function(p,a,c,k,e,d){...}('f=\\'8://7.6/5-4-3-2-1/e.0\\';...',
         16,16,'m3u8|...|source'.split('|'),0,{}))

che decodifica in:

    f='https://surrit.com/<uuid>/playlist.m3u8';   (master, qualità multiple)
    d='https://surrit.com/<uuid>/720p/video.m3u8'; (media playlist)
    b='https://surrit.com/<uuid>/1080p/video.m3u8';

L'estrattore decodifica il packer, prende la MASTER playlist (playlist.m3u8,
così yt-dlp può scegliere la qualità dal menu) e la restituisce con gli
header richiesti dal CDN (Referer + UA). Nessun browser necessario: il
Cloudflare "Just a moment" che in passato si vedeva su questi siti era
causato dall'IP di uscita WARP del server, non da questi siti.
"""

from __future__ import annotations

import re
import urllib.request
from urllib.parse import urlparse

from bot.extractors.base import BaseExtractor, VideoInfo
from bot.logging_config import get_logger

_log = get_logger("extractor")

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

_SAFE_SCHEMES = ("http", "https")

# Packer di Dean Edwards: eval(function(p,a,c,k,e,d){...}('<body>',<base>,<count>,'<k1>|...'.split('|'),0,{}))
_PACKER_RE = re.compile(
    r"eval\(\s*function\s*\(\s*p\s*,\s*a\s*,\s*c\s*,\s*k\s*,\s*e\s*,\s*d\s*\)\s*\{.*?\}\s*\("
    r"'((?:[^'\\]|\\.)*)'\s*,\s*(\d+)\s*,\s*\d+\s*,\s*'((?:[^'\\]|\\.)*)'\s*\.split\('\|'\)",
    re.S)

# Ordinamento qualità (dal più alto al più basso) per scegliere la variante
# migliore quando la master non c'è.
_RES_ORDER = ("2160", "1440", "1080", "720", "480", "360", "240")


def _check_scheme(url: str) -> None:
    if urlparse(url).scheme not in _SAFE_SCHEMES:
        raise ValueError(f"scheme non consentito: {url}")


def _fetch(url: str, referer: str | None = None) -> str:
    """GET della pagina con header browser (i siti bloccano le request nude)."""
    _check_scheme(url)
    headers = {
        "User-Agent": _UA,
        "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
                   "image/avif,image/webp,*/*;q=0.8"),
        "Accept-Language": "en-US,en;q=0.9,it;q=0.8",
    }
    if referer:
        headers["Referer"] = referer
    req = urllib.request.Request(url, headers=headers)  # nosec B310
    with urllib.request.urlopen(req, timeout=30) as r:  # pi-lens-ignore: S310
        return r.read().decode("utf-8", errors="replace")


def _decode_packers(page: str) -> list[str]:
    """Decodifica tutti i packer Dean-Edwards presenti e torna i body."""
    decoded: list[str] = []
    for m in _PACKER_RE.finditer(page):
        body = m.group(1)
        try:
            base_num = int(m.group(2))
        except ValueError:
            continue
        keys = m.group(3).replace("\\'", "'").split("|")

        def _repl(word: str, base_num: int = base_num, keys: list[str] = keys) -> str:
            try:
                idx = int(word, base_num)
            except ValueError:
                return word
            return keys[idx] if 0 <= idx < len(keys) else word

        decoded.append(re.sub(r"\b[0-9a-z]+\b", lambda mm: _repl(mm.group(0)), body))
    return decoded


def _pick_m3u8(urls: list[str]) -> str | None:
    """Sceglie lo stream migliore tra gli URL m3u8 decodificati.

    Priorità: master playlist su surrit.com → variante media con risoluzione
    più alta → qualsiasi m3u8 surrit. Gli URL degli AD (growcdnssedge/myavlive)
    vengono ignorati: non appartengono al video principale.
    """
    candidates = []
    for u in urls:
        if "surrit.com" not in u:
            continue
        if "/playlist.m3u8" in u:
            return u
        candidates.append(u)
    if not candidates:
        return None
    # Ordina per risoluzione dichiarata nel path (es. /1080p/ o /1280x720/)
    def _res_key(u: str) -> int:
        low = u.lower()
        for i, tag in enumerate(_RES_ORDER):
            if f"/{tag}p" in low or f"x{tag}" in low:
                return i
        return len(_RES_ORDER)
    candidates.sort(key=_res_key)
    return candidates[0]


def _page_title(page: str) -> str:
    """Titolo pulito: preferisce og:title, poi <title> senza suffissi del sito."""
    m = re.search(r'<meta\s+property=["\']og:title["\']\s+content=["\']([^"\']+)["\']',
                  page, re.I)
    if not m:
        m = re.search(r"<title>(.*?)</title>", page, re.S | re.I)
    if not m:
        return "Video"
    title = re.sub(r"\s+", " ", m.group(1)).strip()
    # Rimuovi i suffissi di marca (" - Site", " | Site") tipici di <title>.
    title = re.sub(r"\s*[-|–:]\s*[A-Za-z0-9.]*(missav|123av|surrit|video|jav)\s*$",
                   "", title, flags=re.I).strip()
    return title or "Video"


class SurritExtractor(BaseExtractor):
    DOMAINS: tuple[str, ...] = ("missav.ws", "123av.org")

    async def extract(self, url: str) -> VideoInfo:
        page = _fetch(url)
        decoded = _decode_packers(page)
        if not decoded:
            raise RuntimeError("Nessun packer video trovato nella pagina")

        m3u8_urls: list[str] = []
        for body in decoded:
            m3u8_urls.extend(re.findall(r"https?://[^'\"\s]+?\.m3u8", body))
        stream = _pick_m3u8(m3u8_urls)
        if stream is None:
            raise RuntimeError("Nessuno stream m3u8 (surrit) trovato nella pagina")

        title = _page_title(page)
        _log.info("Surrit: %s -> %s", title[:60], stream[:90])
        return VideoInfo(
            url=stream,
            title=title,
            headers={
                "User-Agent": _UA,
                "Referer": url,
                "Accept": "*/*",
            },
        )
