"""Extractor per la piattaforma "get_file/v-acctoken" (porn4fans.com e
shareanynudes.com — stesso motore, anche su altri sottodomini: it., en., ...).

Il sito serve i video come MP4 diretti protetti da token anti-leech: la
pagina del video contiene URL del tipo

    https://it.porn4fans.com/get_file/1/<hash>/0/<id>/<id>_720p.mp4/?v-acctoken=<token>

(dopo il token, il CDN risponde 403 senza). I file `*_preview_new.mp4` e le
miniatura vanno ignorati. I token hanno una scadenza, quindi l'estrattore
prende il candidato di qualità più alta con token ancora valido (verifica
con una richiesta di range leggera) e lo restituisce con Referer + UA.

Nessun browser necessario; il 403 "Edge IP Restricted" che si vedeva era
causato dall'IP WARP del server, che ora è disattivato.
"""

from __future__ import annotations

import re
import urllib.error
import urllib.request
from urllib.parse import (
    parse_qsl,
    urlencode,
    urlparse,
    urlsplit,
    urlunsplit,
)

from bot.extractors.base import BaseExtractor, VideoInfo
from bot.logging_config import get_logger

_log = get_logger("extractor")

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

_SAFE_SCHEMES = ("http", "https")

# URL video diretti: cattura l'URL COMPLETO (incluso `?v-acctoken=`); i
# file `*_preview_new.mp4` (cover marquee) e quelli senza token vengono
# scartati dopo.
_GET_FILE_RE = re.compile(r"https://[^\"'\\ )]+?/get_file/[^\"'\\ )]+")

# Suffisso di qualità nel nome file: .../<id>_1080p.mp4, _720p.mp4, ...
_QUALITY_RE = re.compile(r"_(\d{3,4})p\.mp4(?:\?|$|/)")

_QUALITY_ORDER = ("2160", "1440", "1080", "720", "540", "480", "360", "240")


def _check_scheme(url: str) -> None:
    if urlparse(url).scheme not in _SAFE_SCHEMES:
        raise ValueError(f"scheme non consentito: {url}")


def _fetch(url: str, referer: str | None = None) -> str:
    """GET della pagina con header browser."""
    _check_scheme(url)
    headers = {
        "User-Agent": _UA,
        "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
                   "image/avif,image/webp,*/*;q=0.8"),
        "Accept-Language": "it-IT,it;q=0.9,en;q=0.8",
    }
    if referer:
        headers["Referer"] = referer
    req = urllib.request.Request(url, headers=headers)  # pi-lens-ignore: S310
    with urllib.request.urlopen(req, timeout=30) as r:  # pi-lens-ignore: S310
        return r.read().decode("utf-8", errors="replace")


def _probe_ok(url: str, referer: str) -> bool:
    """Range request 0-15: True se il CDN serve davvero video (token vivo).

    Un token scaduto risponde con HTML di blocco o un'immagine segnaposto,
    mai con un range di video/mp4.
    """
    _check_scheme(url)
    headers = {
        "User-Agent": _UA,
        "Accept": "*/*",
        "Range": "bytes=0-15",
        "Referer": referer,
    }
    req = urllib.request.Request(url, headers=headers)  # pi-lens-ignore: S310
    try:
        with urllib.request.urlopen(req, timeout=15) as r:  # pi-lens-ignore: S310
            ct = (r.headers.get("Content-Type") or "").lower()
            return r.status in (200, 206) and ct.startswith("video/")
    except (urllib.error.HTTPError, urllib.error.URLError, OSError, ValueError):
        return False


def _resolve_direct_url(url: str, referer: str) -> str:
    """Segue il redirect del CDN e, se finisce su un gateway .php
    (shareanynudes -> sn1.nudes365.com/remote_control.php), ricostruisce
    l'URL mp4 DIRETTO dal parametro ?file= e con gli stessi parametri
    firmati. yt-dlp rifiuta le estensioni non comuni (.php) per sicurezza
    (GHSA-79w7-vh3h-8g4j), quindi deve ricevere un URL .mp4 pulito.
    Se il CDN serve già diretto (porn4fans) l'URL resta invariato.
    """
    _check_scheme(url)
    headers = {
        "User-Agent": _UA,
        "Accept": "*/*",
        "Range": "bytes=0-15",
        "Referer": referer,
    }
    req = urllib.request.Request(url, headers=headers)  # pi-lens-ignore: S310
    try:
        with urllib.request.urlopen(req, timeout=15) as r:  # pi-lens-ignore: S310
            final = r.geturl()
    except (urllib.error.HTTPError, urllib.error.URLError, OSError, ValueError):
        return url
    if "remote_control.php" not in final.lower() and not final.lower().rsplit(".", 1)[-1].startswith("php"):
        return url  # già diretto (porn4fans)

    parts = urlsplit(final)
    query = parse_qsl(parts.query, keep_blank_values=True)
    path = ""
    keep: list[tuple[str, str]] = []
    for key, value in query:
        if key == "file":
            path = value
        else:
            keep.append((key, value))
    if not path:
        return final
    direct = urlunsplit((parts.scheme, parts.netloc, path, urlencode(keep), ""))
    _log.info("Gateway .php -> mp4 diretto: %s", direct[:110])
    return direct


def _quality_of(url: str) -> int:
    """Indice di qualità (0 = più alta) dedotto dal suffisso nel nome file."""
    m = _QUALITY_RE.search(url)
    if not m:
        return len(_QUALITY_ORDER)  # originale senza suffisso: ultimo
    try:
        res = m.group(1)
    except IndexError:
        return len(_QUALITY_ORDER)
    for i, tag in enumerate(_QUALITY_ORDER):
        if res == tag:
            return i
    return len(_QUALITY_ORDER)


def _page_title(page: str) -> str:
    """Titolo pulito: og:title, poi <title> senza i suffissi del sito."""
    m = re.search(r'<meta\s+property=["\']og:title["\']\s+content=["\']([^"\']+)["\']',
                  page, re.I)
    if not m:
        m = re.search(r"<title>(.*?)</title>", page, re.S | re.I)
    if not m:
        return "Video"
    title = re.sub(r"\s+", " ", m.group(1)).strip()
    title = re.sub(r"\s*[-|–:]\s*[A-Za-z0-9.]*(porn4fans|porn)\s*$", "",
                   title, flags=re.I).strip()
    return title or "Video"


class Porn4FansExtractor(BaseExtractor):
    DOMAINS: tuple[str, ...] = ("porn4fans.com", "shareanynudes.com")

    async def extract(self, url: str) -> VideoInfo:
        page = _fetch(url)
        referer = f"{urlparse(url).scheme}://{urlparse(url).netloc}/"

        # Tutte le varianti col token, senza preview e senza duplicati.
        seen: set[str] = set()
        candidates: list[str] = []
        for raw in _GET_FILE_RE.findall(page):
            if "_preview" in raw:
                continue
            if "v-acctoken=" not in raw:
                continue
            stem = raw.split("?v-acctoken=", 1)[0]
            if stem in seen:
                continue
            seen.add(stem)
            candidates.append(raw)
        if not candidates:
            raise RuntimeError("Nessun URL video con token trovato nella pagina")

        # Candidati migliori per qualità; tra pari prende il primo token vivo.
        candidates.sort(key=_quality_of)
        chosen: str | None = None
        for cand in candidates[:3]:
            if _probe_ok(cand, referer):
                chosen = cand
                break
        if chosen is None:
            _log.warning("Porn4Fans: tutti i token candidati sembrano scaduti, uso il migliore")
            chosen = candidates[0]
        # Siti CDN con gateway .php: riscrivi in URL mp4 diretto (yt-dlp
        # rifiuterebbe l'estensione .php per sicurezza)
        chosen = _resolve_direct_url(chosen, referer)

        title = _page_title(page)
        _log.info("Porn4Fans: %s -> %s", title[:60], chosen[-90:])
        return VideoInfo(
            url=chosen,
            title=title,
            headers={
                "User-Agent": _UA,
                "Referer": referer,
                "Accept": "*/*",
            },
            fixed_quality=True,
        )
