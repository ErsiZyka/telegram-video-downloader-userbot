"""InternetChicks extractor: pagina con player multipli (streamtape/voe/...).

internetchicks.com (WordPress) non serve il video direttamente: la pagina
contiene pulsanti "PLAYER 01..0N" che aprono embed di hosting esterni
(streamtape.com, voe.sx, hgcloud.to, playmogo.com, bysezejataos.com,
vidoza, dood, filemoon...). yt-dlp non li supporta piu (`Unsupported
URL` — l'estractor StreamTape e stato rimosso). Quindi:

  1. legge la pagina in HTTP per estrarre gli URL embed e il titolo;
  2. carica l'embed in Chromium headless (Playwright) e intercetta lo
     stream video reale dal CDN dell'hosting (come Beeg/StreamingCommunity);
  3. prova TUTTI i player in ordine e prende il PRIMO che serve davvero
     un video: i mirror muoiono spesso (streamtape purga i file) e alcuni
     pulsanti sono solo link di affiliazione cam (pubblicita).

Lo stream ha qualita fissa: il bot scarica direttamente senza menu qualita.
"""

from __future__ import annotations

import asyncio
import re
import urllib.request
from urllib.parse import urlparse

from bot.extractors.base import PlaywrightVideoExtractor, VideoInfo
from bot.logging_config import get_logger

_log = get_logger("extractor")

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

_SAFE_SCHEMES = ("http", "https")

# Pulsanti "PLAYER 0N": onclick="playEmbed('https://host/e/<id>'); ..."
_EMBED_RE = re.compile(r"""playEmbed\(\s*['"](https?://[^'"]+)['"]\s*\)""")

# Host preferiti: i piu affidabili vengono provati per primi.
_PREFERRED_HOSTS = ("streamtape.com",)

# Filtro anti-pubblicita: gli embed "PLAYER 0N" a volte sono link di
# affiliazione cam (livejasmin/vcmdiawe/fkofficial) o tracker: se il browser
# cattura uno stream pubblicitario va scartato e si passa al player
# successivo, altrimenti il bot scaricherebbe una pubblicita.
_AD_KEYWORDS = (
    "livejasmin",
    "vcmdiawe",
    "crmrc.",
    "fkofficial",
    "tabortalkiliac",
    "lumbarssweepup",
    "subgumgutwise",
    "drivagebidding",
    "juicyads",
    "crococlip",
)


def _check_scheme(url: str) -> None:
    if urlparse(url).scheme not in _SAFE_SCHEMES:
        raise ValueError(f"scheme non consentito: {url}")


def _fetch(url: str) -> str:
    _check_scheme(url)
    headers = {
        "User-Agent": _UA,
        "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"),
        "Accept-Language": "en-US,en;q=0.9",
    }
    req = urllib.request.Request(url, headers=headers)  # pi-lens-ignore: S310
    with urllib.request.urlopen(req, timeout=30) as r:  # pi-lens-ignore: S310
        return r.read().decode("utf-8", errors="replace")


def _find_embeds(page: str) -> list[str]:
    """URL embed ordinati: prima i preferiti (streamtape), poi gli altri."""
    seen: set[str] = set()
    ordered: list[str] = []
    for raw in _EMBED_RE.findall(page):
        if raw in seen:
            continue
        seen.add(raw)
        ordered.append(raw)
    if not ordered:
        return []
    preferred = [e for e in ordered if any(h in e for h in _PREFERRED_HOSTS)]
    rest = [e for e in ordered if e not in preferred]
    return preferred + rest


def _page_title(page: str) -> str:
    m = re.search(r"<title>(.*?)</title>", page, re.S | re.I)
    if not m:
        return "Video"
    title = re.sub(r"\s+", " ", m.group(1)).strip()
    title = re.sub(r"\s*[-|–:]\s*Internet\s*Chicks\s*$", "", title, flags=re.I).strip()
    return title or "Video"


def _is_ad_stream(stream_url: str) -> bool:
    """True se lo stream catturato e' una pubblicita cam/tracker."""
    low = (stream_url or "").lower()
    return any(k in low for k in _AD_KEYWORDS)


class InternetchicksExtractor(PlaywrightVideoExtractor):
    DOMAINS: tuple[str, ...] = ("internetchicks.com",)
    RENDER_WAIT = 6
    AFTER_CLICK_WAIT = 10

    async def extract(self, url: str) -> VideoInfo:
        loop = asyncio.get_running_loop()
        page = await loop.run_in_executor(None, _fetch, url)
        embeds = _find_embeds(page)
        if not embeds:
            raise RuntimeError("Nessun embed video trovato nella pagina")
        title = _page_title(page)
        _log.info(
            "IC: %d embed (%s), titolo %r", len(embeds), embeds[0][:50], title[:60]
        )

        errors: list[str] = []
        for embed in embeds:
            try:
                info = await super().extract(embed)
                if _is_ad_stream(info.url):
                    _log.warning(
                        "IC: stream pubblicitario da %s, passo al player successivo",
                        embed[:50],
                    )
                    errors.append(f"{embed[:40]}: stream ad")
                    continue
                _log.info("IC: stream da %s -> %s", embed[:60], info.url[:80])
                return VideoInfo(
                    url=info.url,
                    title=title or info.title,
                    headers=info.headers,
                    fixed_quality=True,
                )
            except Exception as e:
                errors.append(f"{embed[:40]}: {str(e)[:55]}")
                _log.warning("IC: embed fallito %s (%r)", embed[:60], e)
        raise RuntimeError(
            "Nessun player disponibile per questo video (mirror offline o solo "
            "pubblicita): " + "; ".join(errors[:3])
        )
