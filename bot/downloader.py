"""Wrapper around yt-dlp for video info extraction and downloading."""

QUALITY_FORMATS = {
    "360": "bestvideo[height<=360]+bestaudio/best[height<=360]/best",
    "720": "bestvideo[height<=720]+bestaudio/best[height<=720]/best",
    "1080": "bestvideo[height<=1080]+bestaudio/best[height<=1080]/best",
    "max": "bestvideo+bestaudio/best",
}


class DownloadError(Exception):
    """Raised when a download fails after all retries."""


class ExtractError(Exception):
    """Raised when info extraction fails."""


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
    """Build cookie-related yt-dlp options from COOKIES_FROM_BROWSER env var.

    Set COOKIES_FROM_BROWSER in .env to one of: chrome, edge, firefox, brave,
    chromium, opera, safari, vivaldi, whale.
    """
    browser = os.getenv("COOKIES_FROM_BROWSER", "").strip().lower()
    if not browser:
        return {}
    return {"cookiesfrombrowser": (browser,)}


def extract_info(url: str) -> dict | list[dict]:
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
    url = _normalize_url(url)

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)

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
        if "Private video" in msg or "Video unavailable" in msg:
            raise ExtractError("Video non accessibile (privato o non disponibile).")
        if "This video is not available" in msg:
            raise ExtractError("Video non disponibile (potrebbe essere geo-bloccato).")
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
            ["yt-dlp", "--version"],
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

    format_str = QUALITY_FORMATS[quality]
    output_template = os.path.join("downloads", "%(title).100s.%(ext)s")

    def _make_progress_hook():
        """Create a yt-dlp progress hook that calls our callback."""

        def hook(d: dict) -> None:
            if d["status"] == "downloading":
                downloaded = d.get("downloaded_bytes", 0) or 0
                total = d.get("total_bytes", 0) or d.get("total_bytes_estimate", 0) or 0
                speed = d.get("speed", 0) or 0

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
    }
    ydl_opts.update(_get_ydl_cookie_opts())
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

                return filename

        except yt_dlp.utils.DownloadError as e:
            last_error = str(e)
        except Exception as e:
            last_error = str(e)

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
