"""Wrapper around yt-dlp for video info extraction and downloading."""

QUALITY_FORMATS = {
    # "360" uses height<=480 fallback because some streaming CDNs (streamingcommunity/
    # vixcloud) only offer 480p as the lowest tier — there is no 360p variant.
    "360": "bestvideo[height<=480]+bestaudio/best[height<=480]/bestvideo+bestaudio/best",
    "720": "bestvideo[height<=720]+bestaudio/best[height<=720]/best",
    "1080": "bestvideo[height<=1080]+bestaudio/best[height<=1080]/best",
    "max": "bestvideo+bestaudio/best",
}


class DownloadError(Exception):
    """Raised when a download fails after all retries."""


class _SlowStartError(Exception):
    """Internal: raised from the progress hook when the download is
    suspiciously slow early on (CDN throttling on the first connection).
    The retry loop catches it and retries with more concurrent fragments."""


class ExtractError(Exception):
    """Raised when info extraction fails."""


class VideoUnavailableError(ExtractError):
    """Raised when the video exists but is NOT freely available from the
    source (members-only, private, paywalled, geo-blocked, etc.).

    The bot shows a clear message and does NOT retry with the universal
    fallback: the content is protected, not technically broken. We only
    download what the source serves freely."""


def format_size(size_mb: float) -> str:
    """Format size in human-readable form."""
    if size_mb >= 1024:
        return f"{size_mb / 1024:.1f} GB"
    return f"{size_mb:.1f} MB"


def format_speed(mbps: float) -> str:
    """Format download speed."""
    return f"{mbps:.1f} MB/s"


def format_eta(seconds: int | None | float) -> str:
    """Format ETA as human-readable string."""
    if seconds is None or seconds < 0:
        return "..."
    seconds = int(seconds)
    if seconds == 0:
        return "0s"
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    parts = []
    if hours > 0:
        parts.append(f"{hours}h")
    if minutes > 0:
        parts.append(f"{minutes}m")
    if secs > 0 or not parts:
        parts.append(f"{secs}s")
    return " ".join(parts)


import yt_dlp
import os
import re
import sys
import subprocess
import json

# Slow-start detector tuning (env-overridable)
SLOW_START_GRACE_S = float(os.getenv("SLOW_START_GRACE", "8"))             # seconds to observe
SLOW_START_MIN_BPS = float(os.getenv("SLOW_START_MIN_KB", "250")) * 1024   # bytes/s threshold

from bot.logging_config import get_logger

_log = get_logger("downloader")


# Some yt-dlp extractors only match specific subdomains (e.g. YouPorn matches
# only www.youporn.com or youporn.com). URLs with other subdomains (it., de.,
# etc.) fall back to the generic extractor which often grabs the wrong asset
# (e.g. a 0-byte SVG avatar). Normalize known subdomains to the canonical one.
_URL_NORMALIZERS = [
    # youporn: any subdomain -> www.youporn.com
    (re.compile(r'^https?://(?!www\.)[a-z]{2,}\.youporn\.com/', re.IGNORECASE),
     lambda m: m.group(0).replace(m.group(0).split('//')[1].split('.')[0] + '.', 'www.', 1)),
]


def _normalize_url(url: str) -> str:
    """Rewrite known subdomain issues so yt-dlp picks the right extractor."""
    for pattern, repl in _URL_NORMALIZERS:
        if pattern.search(url):
            # replace the subdomain with www.
            head = url.split('//', 1)
            rest = head[1]
            sub, _, remainder = rest.partition('.')
            url = head[0] + '//www.' + remainder
    return url


def _get_ydl_cookie_opts() -> dict:
    """Build cookie-related yt-dlp options from environment variables.

    Supports:
      - COOKIES_FILE: path to a cookies.txt file
      - COOKIES_FROM_BROWSER: browser name (e.g. 'chrome', 'firefox')
    """
    opts = {}
    
    # 1. First priority: cookies file (very reliable on headless servers)
    cookies_file = os.getenv("COOKIES_FILE", "").strip()
    if cookies_file:
        if os.path.exists(cookies_file):
            opts["cookiefile"] = cookies_file
            _log.info("Caricamento cookie dal file: %s", cookies_file)
            return opts
        else:
            _log.warning("Il file dei cookie specificato non esiste: %s", cookies_file)

    # 2. Second priority: browser cookies
    browser = os.getenv("COOKIES_FROM_BROWSER", "").strip().lower()
    if browser:
        _log.info("Tentativo di estrazione cookie dal browser: %s", browser)
        opts["cookiesfrombrowser"] = (browser,)
        
    return opts


_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


_UA_FALLBACKS = [
    _BROWSER_UA,
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64; rv:127.0) Gecko/20100101 Firefox/127.0",
]


def _get_browser_headers(ua: str = _BROWSER_UA) -> dict:
    """Realistic browser headers for sites that block bare HTTP requests."""
    return {
        "User-Agent": ua,
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,*/*;q=0.8"
        ),
        "Accept-Language": "it-IT,it;q=0.9,en-US;q=0.8,en;q=0.7",
        "Cache-Control": "no-cache",
    }


def _is_blocked_error(msg: str) -> bool:
    """True if a yt-dlp error looks like a bot/403 block worth retrying with browser headers."""
    lowered = (msg or "").lower()
    return any(k in lowered for k in (
        "403", "forbidden", "http error 400", "bad request",
        "cloudflare", "captcha", "access denied", "blocked",
        # aria2c esce con codice 22 su qualunque risposta HTTP >= 400 senza
        # stampare lo status: i CDN di bigfuck.tv/bustybus.com rispondono così
        # (403) agli URL token-gated estratti da yt-dlp.
        "aria2c exited with code 22",
    ))


def _extract_with_retry(url: str, ydl_opts: dict, headers_used: dict | None = None):
    """Run yt-dlp extract_info; retry once with browser headers on 403/block errors."""
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            return ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as e:
        msg = str(e)
        if _is_blocked_error(msg) and not (headers_used or {}).get("User-Agent"):
            _log.warning("Possibile blocco bot (%s): riprovo con header browser", msg[:90])
            retry_opts = dict(ydl_opts)
            retry_opts["http_headers"] = _get_browser_headers()
            with yt_dlp.YoutubeDL(retry_opts) as ydl:
                return ydl.extract_info(url, download=False)
        raise


def _get_network_opts() -> dict:
    """Build network options (IPv4, impersonation, extractor-args) to prevent 403 blocks."""
    opts = {}
    
    # Force IPv4 by default (helps bypass YouTube IPv6 403 blocks)
    force_ipv4_env = os.getenv("FORCE_IPV4", "true").strip().lower()
    if force_ipv4_env in ("true", "1", "yes"):
        opts["force_ipv4"] = True
        _log.debug("Forzo l'utilizzo di IPv4 per le connessioni di yt-dlp")

    # Impersonate a real browser TLS fingerprint via curl_cffi. Many CDNs
    # (Cloudflare, Akamai...) block plain python-requests TLS signatures with
    # HTTP 403; impersonating Chrome avoids that without per-site hacks.
    impersonate = os.getenv("IMPERSONATE", "chrome").strip().lower()
    if impersonate not in ("", "none", "off", "false", "0"):
        try:
            import curl_cffi  # noqa: F401
            from yt_dlp.networking.impersonate import ImpersonateTarget
            opts["impersonate"] = ImpersonateTarget.from_str(impersonate)
            _log.info("Impersonamento TLS attivo: %s", impersonate)
        except ImportError:
            _log.warning("curl_cffi non disponibile: impersonamento disattivato")
        except Exception as e:
            _log.warning("Impersonamento non disponibile (%s): disattivato", repr(e))

    # Add Youtube extractor arguments to bypass restricted clients
    opts["extractor_args"] = {
        "youtube": {
            "player_client": "web,mweb",
        }
    }
    
    return opts


def _get_downloader_opts() -> dict:
    """Build downloader-related options (like aria2c and concurrent fragments)."""
    opts = {}
    
    # 1. Concurrent fragment downloads (native HLS/DASH multi-threading)
    concurrent_fragments = os.getenv("CONCURRENT_FRAGMENTS", "16").strip()
    try:
        opts["concurrent_fragment_downloads"] = int(concurrent_fragments)
        _log.info("Impostato concurrent_fragment_downloads a %d", opts["concurrent_fragment_downloads"])
    except ValueError:
        opts["concurrent_fragment_downloads"] = 16
        _log.warning("Valore CONCURRENT_FRAGMENTS non valido, uso il default: 16")

    # 2. HTTP chunk size for the native downloader (speeds up single-file CDN pulls)
    chunk = os.getenv("HTTP_CHUNK_SIZE", "").strip()
    if chunk:
        opts["http_chunk_size"] = chunk
        _log.info("http_chunk_size impostato a %s", chunk)

    # 3. External downloader (e.g. aria2c for direct HTTP/FTP files)
    use_aria2 = os.getenv("USE_ARIA2", "true").strip().lower() in ("true", "1", "yes")
    if use_aria2:
        import shutil
        if shutil.which("aria2c"):
            # Map protocols: use aria2c by default but fall back to native for HLS/DASH
            opts["external_downloader"] = {
                "default": "aria2c",
                "m3u8": "native",
                "dash": "native",
            }
            opts["external_downloader_args"] = {
                "aria2c": [
                    "-c",
                    "-j", "16",
                    "-x", "16",
                    "-s", "16",
                    "-k", "1M",
                    "--file-allocation=none",
                    "--console-log-level=warn",
                    "--summary-interval=0"
                ]
            }
            _log.info("Abilitato downloader esterno aria2c per download direct HTTP")
        else:
            _log.debug("aria2c non trovato nel sistema, uso il downloader nativo")
            
    return opts


def extract_info(url: str, extra_headers: dict | None = None) -> dict | list[dict]:
    """
    Extract video/playlist metadata WITHOUT downloading.

    Returns:
        Single video: dict with keys: title, duration, thumbnail, webpage_url,
                      uploader, filesize_approx
        Playlist: list of dicts, each with: title, duration, thumbnail,
                  webpage_url, uploader

    Raises:
        ExtractError: if the URL is not supported or video is unavailable.
    """
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": "in_playlist",
        "skip_download": True,
    }
    ydl_opts.update(_get_ydl_cookie_opts())
    ydl_opts.update(_get_network_opts())
    if extra_headers:
        ydl_opts["http_headers"] = dict(extra_headers)
    url = _normalize_url(url)

    try:
        info = _extract_with_retry(url, ydl_opts, extra_headers)

        if info is None:
            raise ExtractError("Nessuna informazione trovata per questo link.")

        # Playlist detection: has 'entries' key
        if "entries" in info:
            entries = info["entries"]
            if entries is None:
                return []

            videos = []
            for entry in entries:
                if entry is None:
                    continue
                videos.append({
                    "title": entry.get("title", "Sconosciuto"),
                    "duration": entry.get("duration", 0),
                    "thumbnail": entry.get("thumbnail", ""),
                    "webpage_url": entry.get("webpage_url", entry.get("url", "")),
                    "uploader": entry.get("uploader", ""),
                })
            return videos

        # Single video
        return {
            "title": info.get("title", "Sconosciuto"),
            "duration": info.get("duration", 0),
            "thumbnail": info.get("thumbnail", ""),
            "webpage_url": info.get("webpage_url", url),
            "uploader": info.get("uploader", ""),
            "filesize_approx": info.get("filesize_approx", 0),
        }

    except yt_dlp.utils.DownloadError as e:
        msg = str(e)
        lowered = msg.lower()
        # Content that is not freely available -> clear message, no fallback.
        if any(k in lowered for k in (
                "members-only", "channel's members", "members on level",
                "join this channel", "premium", "purchase", "paywall",
                "sign in", "login", "logged in", "account", "subscription")):
            raise VideoUnavailableError(
                "Video riservato a membri/abbonati del canale (contenuto a pagamento): "
                "non è liberamente disponibile dalla sorgente, quindi non lo scarico.")
        if "private video" in lowered or "video unavailable" in lowered:
            raise VideoUnavailableError("Video non accessibile (privato o non disponibile).")
        if "this video is not available" in lowered:
            raise VideoUnavailableError("Video non disponibile (potrebbe essere geo-bloccato).")
        raise ExtractError(f"Link non supportato o video non disponibile: {msg}")
    except Exception as e:
        raise ExtractError(f"Errore durante l'estrazione: {str(e)}")


import subprocess
import time
import json


def probe_video_metadata(filepath: str) -> tuple[int, int, int]:
    """Extract real (duration_seconds, width, height) from a video file via ffprobe.

    Returns (0, 0, 0) if ffprobe is unavailable or the file cannot be probed.
    Telegram needs real values to show the file as a playable video (with
    streaming + thumbnail) instead of a generic document.
    """
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_streams", filepath],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            return (0, 0, 0)
        data = json.loads(result.stdout)
        for stream in data.get("streams", []):
            if stream.get("codec_type") == "video":
                duration = float(stream.get("duration", 0) or 0)
                width = int(stream.get("width", 0) or 0)
                height = int(stream.get("height", 0) or 0)
                _log.info("ffprobe: %dx%d %ds", width, height, int(duration))
                return (int(duration), width, height)
    except (FileNotFoundError, json.JSONDecodeError, subprocess.TimeoutExpired, OSError):
        pass
    return (0, 0, 0)


def check_dependencies() -> tuple[bool, str]:
    """
    Verify yt-dlp and ffmpeg are installed.

    Returns:
        (ok, error_message) — ok is True if all deps present.
    """
    try:
        result = subprocess.run(
            [sys.executable, "-m", "yt_dlp", "--version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return False, "yt-dlp non è installato. Installa con: pip install yt-dlp"
    except FileNotFoundError:
        return False, "yt-dlp non è installato. Installa con: pip install yt-dlp"

    try:
        result = subprocess.run(
            ["ffmpeg", "-version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return False, "ffmpeg non è installato. Installa da: https://ffmpeg.org/download.html"
    except FileNotFoundError:
        return False, "ffmpeg non è installato. Installa da: https://ffmpeg.org/download.html"

    return True, ""


def download_video(
    url: str,
    quality: str,
    progress_callback,
    max_retries: int = 3,
    extra_headers: dict | None = None,
) -> str:
    """
    Download a video at the specified quality with retry logic.

    Args:
        url: Video URL
        quality: One of "360", "720", "1080", "max"
        progress_callback: callable(downloaded_mb, total_mb, speed_mbps, eta_seconds)
        max_retries: Number of download attempts before giving up

    Returns:
        Path to the downloaded file

    Raises:
        DownloadError: if download fails after all retries
        ValueError: if quality is invalid
    """
    if quality not in QUALITY_FORMATS:
        raise ValueError(f"Qualità non valida: {quality}. Usa: {list(QUALITY_FORMATS.keys())}")

    # supjav.com: HLS protetto da MOUFLON (live relay con token a scadenza).
    # yt-dlp non lo supporta: serve il recorder dedicato che registra i
    # segmenti in tempo reale man mano che il relay li genera.
    if _is_mouflon_url(url):
        return _download_mouflon_relay(url, quality, progress_callback, extra_headers)

    format_str = QUALITY_FORMATS[quality]
    _log.info("Download richiesto: %s qualità=%s format=%s", url[:80], quality, format_str)
    output_template = os.path.join("downloads", "%(title).100s.%(ext)s")

    def _make_progress_hook():
        """Create a yt-dlp progress hook that calls our callback.

        Also implements the slow-start detector: if after a grace period the
        download is clearly throttled (single-digit KB/s), raise _SlowStartError
        so the retry loop can restart with more concurrent connections.
        """
        slow_started = None
        slow_prev_downloaded = 0

        def hook(d: dict) -> None:
            nonlocal slow_started, slow_prev_downloaded
            if d["status"] == "downloading":
                downloaded = d.get("downloaded_bytes", 0) or 0
                total = d.get("total_bytes", 0) or d.get("total_bytes_estimate", 0) or 0
                speed = d.get("speed", 0) or 0

                # Slow-start detector: scatta solo nei secondi iniziali del
                # trasferimento, quando ENTRAMBI la media e la velocità
                # istantanea sono ben sotto soglia. Due guardie evitano i
                # falsi positivi vidi sui download DASH (video+audio):
                #  - per l'audio yt-dlp riparte da byte 0: se il contatore
                #    salta all'indietro ricomincia anche la finestra di osserva-
                #    zione (prima il detector uccideva il download al ~95%);
                #  - durante il merge/tra le fasi gli hook si fermano da soli,
                #    quindi richiediamo anche che la velocità istantanea
                #    riportata da yt-dlp sia bassa per dichiarare throttle.
                now = time.monotonic()
                if slow_started is None or downloaded + 1024 * 1024 < slow_prev_downloaded:
                    slow_started = now
                slow_prev_downloaded = downloaded
                elapsed = now - slow_started
                if (elapsed >= SLOW_START_GRACE_S
                        and downloaded < SLOW_START_MIN_BPS * elapsed
                        and speed * 4 < SLOW_START_MIN_BPS):
                    rate = downloaded / elapsed / 1024 if elapsed > 0 else 0
                    _log.warning("Partenza lenta: %.0f KB/s dopo %.0fs -> riavvio con più connessioni", rate, elapsed)
                    raise _SlowStartError("slow start")

                downloaded_mb = downloaded / (1024 * 1024)
                total_mb = total / (1024 * 1024) if total else 0
                speed_mbps = speed / (1024 * 1024) if speed else 0
                eta = d.get("eta")

                progress_callback(downloaded_mb, total_mb, speed_mbps, eta)

        return hook

    ydl_opts = {
        "format": format_str,
        "outtmpl": output_template,
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
        "progress_hooks": [_make_progress_hook()],
        "socket_timeout": 30,
        "retries": 5,
        "fragment_retries": 5,
    }
    ydl_opts.update(_get_ydl_cookie_opts())
    ydl_opts.update(_get_network_opts())
    ydl_opts.update(_get_downloader_opts())
    if extra_headers:
        ydl_opts["http_headers"] = dict(extra_headers)
    url = _normalize_url(url)

    last_error = None

    for attempt in range(1, max_retries + 1):
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                filename = ydl.prepare_filename(info)
                # After merge, the extension might be .mp4 or .mkv
                if not os.path.exists(filename):
                    base = os.path.splitext(filename)[0]
                    if os.path.exists(base + ".mp4"):
                        filename = base + ".mp4"
                    elif os.path.exists(base + ".mkv"):
                        filename = base + ".mkv"

                _log.info("Download completato: %s", filename)
                return filename

        except _SlowStartError:
            last_error = "slow start"
            # Boost parallelism: each slow start doubles the concurrent
            # fragments (up to 64) so throttled per-connection CDNs still
            # aggregate decent throughput.
            cur = int(ydl_opts.get("concurrent_fragment_downloads", 16) or 16)
            boosted = min(cur * 2, 64)
            ydl_opts["concurrent_fragment_downloads"] = boosted
            _log.info("Riavvio con %d fragment concorrenti (era %d)", boosted, cur)
        except yt_dlp.utils.DownloadError as e:
            last_error = str(e)
            # Some streaming CDNs offer discrete quality tiers (480/720/1080)
            # and don't have a 360p variant. If the requested quality is not
            # available, fall back to "best" once so the user still gets a video.
            if "Requested format is not available" in last_error and ydl_opts.get("format") != "best":
                _log.warning("Formato non disponibile, fallback a 'best' per %s", url[:80])
                fallback_opts = dict(ydl_opts)
                fallback_opts["format"] = "best"
                try:
                    with yt_dlp.YoutubeDL(fallback_opts) as ydl:
                        info = ydl.extract_info(url, download=True)
                        filename = ydl.prepare_filename(info)
                        if not os.path.exists(filename):
                            base = os.path.splitext(filename)[0]
                            if os.path.exists(base + ".mp4"):
                                filename = base + ".mp4"
                            elif os.path.exists(base + ".mkv"):
                                filename = base + ".mkv"
                        return filename
                except yt_dlp.utils.DownloadError as e2:
                    last_error = str(e2)
                except Exception as e2:
                    last_error = str(e2)
        except Exception as e:
            last_error = str(e)

        # Bot/403 block? Retry with realistic browser headers (helps many CDNs).
        if _is_blocked_error(last_error) and not (extra_headers or {}).get("User-Agent"):
            _log.warning("Possibile blocco bot (%s): riprovo con header browser", last_error[:90])
            ydl_opts["http_headers"] = _get_browser_headers()

        if attempt < max_retries:
            wait = 2 ** (attempt - 1)  # 1s, 2s, 4s
            time.sleep(wait)

    # Cleanup any partial file
    cleanup_orphan_files()

    raise DownloadError(
        f"Download fallito dopo {max_retries} tentativi: {last_error}"
    )


def cleanup_orphan_files(download_dir: str = "downloads") -> list[str]:
    """
    Remove all files from the download directory.

    Returns list of removed file paths.
    """
    removed = []
    if os.path.isdir(download_dir):
        for f in os.listdir(download_dir):
            path = os.path.join(download_dir, f)
            if os.path.isfile(path) and f != ".gitkeep":
                try:
                    os.remove(path)
                    removed.append(path)
                except (PermissionError, OSError):
                    pass  # file locked by another process, skip
    return removed


# ─── supjav.com: MOUFLON live-relay downloader ─────────────────────────────
# supjav streams its videos as a Cloudflare-protected HLS "live relay" on
# growcdnssedge.com with the proprietary MOUFLON anti-hotlink scheme:
#   - the media playlist only exposes a rolling window of ~3 segments;
#   - the plain segment lines are 404 placeholders (media.mp4);
#   - the ONLY working segment URLs are in "#EXT-X-MOUFLON:URI:" lines;
#   - segment URLs embed an expiry token, so each segment must be fetched
#     as soon as it appears in the playlist.
# This downloader polls the playlist, downloads each new segment in real
# time, and writes a local m3u8 that ffmpeg can mux into a single mp4.

_MOUFLON_PSCH_RE = re.compile(r"#EXT-X-MOUFLON:PSCH:v2:(\S+)")
_MOUFLON_URI_RE = re.compile(r"#EXT-X-MOUFLON:URI:(\S+)")
_MOUFLON_MAP_RE = re.compile(r'#EXT-X-MAP:URI="([^"]+)"')
_MOUFLON_MEDIA_SEQ_RE = re.compile(r"EXT-X-MEDIA-SEQUENCE:(\d+)")
_MOUFLON_SEG_SEQ_RE = re.compile(r"_h264_(\d+)_")


def _is_mouflon_url(url: str) -> bool:
    return "growcdnssedge.com" in (url or "").lower() and ".m3u8" in (url or "").lower()


def _http_get_bytes(url: str, headers: dict, timeout: int = 20) -> bytes:
    """GET with UA+Referer (required by the supjav CDN)."""
    import urllib.request
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _pick_mouflon_quality(variants: list[tuple[str, str]], quality: str) -> str:
    """variants: [(name like '240p'/'480p', url)]. Map the bot menu quality
    (360/720/1080/max) to the closest available variant."""
    if not variants:
        return ""
    heights = {int(n[:-1]): u for n, u in variants}
    target = {"360": 360, "720": 720, "1080": 1080, "max": 99999}.get(quality, 720)
    chosen = None
    for h in sorted(heights):
        if h <= target:
            chosen = h
    if chosen is None:
        chosen = min(heights)
    return heights[chosen]


def _download_mouflon_relay(
    master_or_media_url: str,
    quality: str,
    progress_callback,
    extra_headers: dict | None,
) -> str:
    """Download a supjav MOUFLON live-relay video.

    Records the relay from the CURRENT position for as long as new segments
    keep arriving (or until ENDLIST / idle timeout / max record time).
    """
    import time
    import tempfile
    import threading
    import shutil
    from concurrent.futures import ThreadPoolExecutor, as_completed

    headers = {
        "User-Agent": (extra_headers or {}).get("User-Agent")
        or _STREAM_HEADERS["User-Agent"],
        "Referer": (extra_headers or {}).get("Referer") or "https://supjav.com/",
        "Accept": "*/*",
    }

    max_record = int(os.getenv("SUPJAV_MAX_RECORD_SEC", "5400"))  # default 90 min
    idle_stop = int(os.getenv("SUPJAV_IDLE_SEC", "25"))           # relay ended?

    # ── resolve master → variant + pkey ────────────────────────────────────
    if "/master/" in master_or_media_url:
        master_text = _http_get_bytes(master_or_media_url, headers).decode()
        pkeys = _MOUFLON_PSCH_RE.findall(master_text)
        variants = re.findall(
            r'#EXT-X-STREAM-INF:[^\n]*NAME="(\d+p)"\s*\n(\S+)', master_text
        )
        media_url = _pick_mouflon_quality(variants, quality)
        if not media_url:
            raise DownloadError("supjav: nessuna variante video trovata")
    else:
        # already a media playlist URL (possibly with ?psch=v2&pkey=...)
        media_url = master_or_media_url
        pkeys = []
        m = re.search(r"pkey=(\S+)", master_or_media_url)
        if m:
            pkeys = [m.group(1)]
        # senza pkey esplicito: risali al video id e ricava il master per i PSCH
        if not pkeys:
            vm = re.search(r"/b-hls-\d+/(\d+)/", master_or_media_url)
            if vm:
                vid = vm.group(1)
                try:
                    master_text = _http_get_bytes(
                        f"https://edge-hls.growcdnssedge.com/hls/{vid}/master/{vid}.m3u8",
                        headers).decode()
                    pkeys = _MOUFLON_PSCH_RE.findall(master_text)
                    _log.info("supjav: pkey derivati dal master (%d) per id %s", len(pkeys), vid)
                except Exception as e:
                    _log.warning("supjav: master derivato non accessibile: %s", e)
        # pulisci eventuali psch/pkey già presenti nell'URL per evitare duplicati
        media_url = re.sub(r"[?&]psch=v2&pkey=\S+", "", media_url)
        media_url = re.sub(r"[?&]pkey=\S+", "", media_url)

    def _media_url_with_pkey(pkey: str) -> str:
        sep = "&" if "?" in media_url else "?"
        return f"{media_url}{sep}psch=v2&pkey={pkey}"

    # ── poll loop: collect + download new segments in real time ────────────
    tmpdir = tempfile.mkdtemp(prefix="supjav_")
    segments: dict[int, tuple[str, str]] = {}  # seq -> (url, local file)
    init_url: str | None = None
    init_file: str | None = None
    pkey_idx = 0
    quiet_since: float | None = None
    start = time.time()
    dl_lock = threading.Lock()
    downloaded_bytes = 0
    speed_win = []

    try:
        while True:
            if time.time() - start > max_record:
                _log.info("supjav: max record time raggiunto (%ds)", max_record)
                break

            # fetch media playlist (rotate pkey if it stops leaking URIs)
            text = ""
            for attempt in range(len(pkeys) + 1):
                pk = pkeys[pkey_idx % len(pkeys)] if pkeys else ""
                try:
                    text = _http_get_bytes(_media_url_with_pkey(pk), headers).decode()
                except Exception as e:
                    _log.warning("supjav: fetch media fallito (%s), pkey %s", e, pk)
                    text = ""
                if _MOUFLON_URI_RE.search(text):
                    break
                pkey_idx += 1
                if not pkeys:
                    break
            if not _MOUFLON_URI_RE.search(text):
                # pkey rotation esaurita
                if not text:
                    break
                _log.warning("supjav: playlist senza URI MOUFLON, pkey ruotate")

            # init segment
            m = _MOUFLON_MAP_RE.search(text)
            if m:
                init_url = m.group(1)
            if init_url and init_file is None:
                init_file = os.path.join(tmpdir, "init.mp4")
                data = _http_get_bytes(init_url, headers)
                with open(init_file, "wb") as f:
                    f.write(data)
                downloaded_bytes += len(data)
                if progress_callback:
                    progress_callback(downloaded_bytes / 1048576, 0, 0, None)

            # new segments
            new_uris = []
            for uri in _MOUFLON_URI_RE.findall(text):
                mm = _MOUFLON_SEG_SEQ_RE.search(uri)
                seq = int(mm.group(1)) if mm else len(segments) + len(new_uris)
                if seq not in segments:
                    segments[seq] = (uri, "")
                    new_uris.append((seq, uri))

            if new_uris:
                quiet_since = None
                with ThreadPoolExecutor(max_workers=6) as ex:
                    futs = {
                        ex.submit(
                            _http_get_bytes, uri, headers,
                        ): seq for seq, uri in new_uris
                    }
                    for fut in as_completed(futs):
                        seq = futs[fut]
                        try:
                            data = fut.result()
                        except Exception as e:
                            _log.warning("supjav: segmento %s fallito: %s", seq, e)
                            continue
                        fname = os.path.join(tmpdir, f"seg_{seq:08d}.mp4")
                        with open(fname, "wb") as f:
                            f.write(data)
                        with dl_lock:
                            segments[seq] = (segments[seq][0], fname)
                            downloaded_bytes += len(data)
                            speed_win.append((time.time(), len(data)))
                if progress_callback:
                    now = time.time()
                    speed_win[:] = [(t, b) for t, b in speed_win if now - t <= 5]
                    speed = (
                        sum(b for _, b in speed_win) / 5 / 1048576
                        if speed_win else 0
                    )
                    progress_callback(downloaded_bytes / 1048576, 0, speed, None)
            else:
                if quiet_since is None:
                    quiet_since = time.time()
                elif time.time() - quiet_since > idle_stop:
                    _log.info("supjav: relay terminato (nessun segmento per %ds)", idle_stop)
                    break

            if "#EXT-X-ENDLIST" in text:
                _log.info("supjav: ENDLIST trovato")
                break

            time.sleep(3)

        if not segments or not init_file:
            raise DownloadError("supjav: nessun segmento scaricato (relay vuoto?)")

        # ── mux: local m3u8 (init + segments reali scaricati) + ffmpeg ─────
        local_m3u8 = os.path.join(tmpdir, "playlist.m3u8")
        lines = ["#EXTM3U", "#EXT-X-VERSION:6", "#EXT-X-TARGETDURATION:4"]
        lines.append(f'#EXT-X-MAP:URI="{init_file}"')
        for seq in sorted(segments):
            fname = segments[seq][1]
            if not fname:
                continue
            lines.append("#EXTINF:2.0,")
            lines.append(fname)
        lines.append("#EXT-X-ENDLIST")
        with open(local_m3u8, "w") as f:
            f.write("\n".join(lines) + "\n")

        vid_m = re.search(r"/(\d+)/(?:\d+_)?(?:\d+p\.m3u8|.*)", media_url)
        vid_label = f"_{vid_m.group(1)}" if vid_m else ""
        out_base = os.path.join("downloads", f"supjav{vid_label}_{int(time.time())}")
        raw_out = out_base + ".mp4"
        os.makedirs("downloads", exist_ok=True)
        result = subprocess.run(
            [
                "ffmpeg", "-y", "-v", "error",
                "-protocol_whitelist", "file,http,https,tcp,tls,crypto,data",
                "-i", local_m3u8,
                "-c", "copy", "-movflags", "+faststart",
                raw_out,
            ],
            capture_output=True, text=True, timeout=600,
        )
        if result.returncode != 0 or not os.path.exists(raw_out):
            _log.error("supjav: ffmpeg fallito: %s", result.stderr[-400:])
            raise DownloadError(f"supjav: merge ffmpeg fallito: {result.stderr[-200:]}")
        _log.info("supjav: video ok %s (%d segments)", raw_out, len(segments))
        return raw_out
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
