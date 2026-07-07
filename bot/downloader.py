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
