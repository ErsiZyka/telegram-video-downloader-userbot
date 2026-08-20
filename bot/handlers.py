"""Message handlers for the video downloader userbot (Telethon + FastTelethon).

Userbot interaction model: user sends a link -> bot shows a numbered menu ->
user types the number -> bot acts. State is tracked per-user.
Upload uses FastTelethon for parallel TCP connections (10-20 MB/s).
"""

import re
import os
import asyncio
import time as time_module
from urllib.parse import urlparse
from telethon import events
from telethon.errors import FloodWaitError
from telethon.tl.types import DocumentAttributeVideo

from bot.whitelist import Whitelist
from bot.extractors import get_extractor, get_universal_fallback
from bot.downloader import (
    extract_info,
    download_video,
    probe_video_metadata,
    DownloadError,
    ExtractError,
    VideoUnavailableError,
    format_size,
    format_speed,
    format_eta,
    cleanup_orphan_files,
)
from bot.history import DownloadHistory
from bot.fasttelethon import upload_file
from bot.queue import DownloadQueue

URL_REGEX = r"(https?://[^\s]+)"

# ─── State ───
_cancel_requested = False
_edit_muted_until = 0.0

_pending: dict[int, dict] = {}
PENDING_TIMEOUT = 600

_history: DownloadHistory | None = None

_queue: DownloadQueue | None = None
_current_item: dict | None = None
_queue_worker_task: asyncio.Task | None = None
_resume_event: asyncio.Event | None = None
_resume_decision: str | None = None  # "yes" | "no"
_extract_lock = asyncio.Lock()

QUALITY_CHOICES = {
    "1": ("360p", "360"),
    "2": ("720p", "720"),
    "3": ("1080p", "1080"),
    "4": ("MAX", "max"),
}


class CancelDownload(Exception):
    pass


class SlowUploadError(Exception):
    pass


def _get_history() -> DownloadHistory:
    global _history
    if _history is None:
        _history = DownloadHistory(filepath="data/download_history.json")
    return _history


def _get_queue() -> DownloadQueue:
    global _queue
    if _queue is None:
        _queue = DownloadQueue(filepath="data/queue.json")
    return _queue


def _log(msg: str) -> None:
    """Emit a log line through the centralized logger (timestamped, file + console)."""
    try:
        from bot.logging_config import bot_log
        bot_log(msg)
    except Exception:
        pass


def _set_pending(user_id: int, state: dict) -> None:
    state["ts"] = time_module.time()
    _pending[user_id] = state


def _get_pending(user_id: int) -> dict | None:
    state = _pending.get(user_id)
    if not state:
        return None
    if time_module.time() - state.get("ts", 0) > PENDING_TIMEOUT:
        _pending.pop(user_id, None)
        return None
    return state


def _clear_pending(user_id: int) -> None:
    _pending.pop(user_id, None)


def extract_url(text: str) -> str | None:
    match = re.search(URL_REGEX, text)
    if not match:
        return None
    url = match.group(1)
    # Strip trailing punctuation that often gets pasted along with the URL
    # (quotes, commas, periods, closing parens/brackets, colons, semicolons).
    url = url.rstrip("\"'.,);:]")
    return url


def _escape_md(text: str) -> str:
    if not text:
        return ""
    for ch in ("\\", "*", "_", "`", "[", "]"):
        text = text.replace(ch, f"\\{ch}")
    return text


def _build_caption(title: str, url: str = "") -> str:
    """Build the video caption: title + clickable link to the original video and site.

    Markdown inline links are used so they render as t.me-style clickable links.
    """
    caption = _escape_md(title) if title else "Video scaricato"
    if url:
        clean_url = url.strip()
        # Telegram inline link: [text](url). Escape ']' and ')' inside the URL
        # is not needed for the URL part itself, but avoid whitespace breaking it.
        caption += f"\n\n🔗 [Video originale]({clean_url})"
        site = urlparse(clean_url).netloc
        if site.startswith("www."):
            site = site[4:]
        if site:
            caption += f"\n🌐 Sito: {_escape_md(site)}"
    return caption


# ─── Safe message helpers (Telethon API + FloodWait mute) ───

async def _safe_edit(msg, text: str) -> None:
    if msg is None:
        return
    global _edit_muted_until
    now = time_module.time()
    if now < _edit_muted_until:
        return
    try:
        await msg.edit(text)
    except FloodWaitError as e:
        _edit_muted_until = now + e.seconds
        _log(f"Edit FloodWait: muto per {e.seconds}s")
    except Exception as e:
        _log(f"edit fallito: {e!r}")


async def _safe_reply(event, text: str):
    """Reply via event. Returns the sent message or None."""
    global _edit_muted_until
    now = time_module.time()
    if now < _edit_muted_until:
        return None
    try:
        return await event.reply(text)
    except FloodWaitError as e:
        _edit_muted_until = now + e.seconds
        _log(f"Reply FloodWait: muto per {e.seconds}s")
        return None
    except Exception as e:
        _log(f"reply fallito: {e!r}")
        return None


async def _safe_delete(msg) -> None:
    try:
        await msg.delete()
    except Exception as e:
        _log(f"delete fallito: {e!r}")


# ─── Menu senders ───

async def _send_quality_menu(event, user_id: int, url: str, title: str, headers: dict | None = None) -> None:
    menu = await _safe_reply(event,
        f"🎬 **{_escape_md(title)}**\n\n"
        f"Scrivi qui il NUMERO della qualità:\n"
        f"1️⃣ 360p\n2️⃣ 720p\n3️⃣ 1080p\n🔥 4️⃣ MAX"
    )
    if menu is None:
        _log("Menu qualità non inviato (FloodWait?)")
        return
    _set_pending(user_id, {"type": "quality", "url": url, "title": title,
                           "headers": headers or {}})
    _log(f"Menu qualità inviato a {user_id}")


async def _send_playlist_menu(event, user_id: int, videos: list, url: str, page: int = 0) -> None:
    total = len(videos)
    page_size = 10
    total_pages = (total + page_size - 1) // page_size or 1
    start = page * page_size
    page_videos = videos[start:start + page_size]

    lines = [f"📋 **Playlist**: {total} video (pagina {page + 1}/{total_pages})\n"]
    lines.append("_Scrivi il NUMERO del video da scaricare:_\n")
    for i, v in enumerate(page_videos):
        idx = start + i + 1
        vtitle = _escape_md((v.get("title") or "Sconosciuto")[:55])
        dur = v.get("duration", 0)
        dur_str = f" • {dur // 60}:{dur % 60:02d}" if dur else ""
        lines.append(f"{idx}. {vtitle}{dur_str}")
    if total_pages > 1:
        lines.append("\nScrivi `next` o `prev` per cambiare pagina.")

    menu = await _safe_reply(event, "\n".join(lines))
    if menu is None:
        return
    _set_pending(user_id, {"type": "playlist", "videos": videos, "url": url, "page": page})
    _log(f"Menu playlist inviato a {user_id} ({total} video)")


# ─── Core: download_and_upload (FastTelethon parallel upload) ───

async def download_and_upload(
    client, status_msg, url: str, quality: str, title: str,
    channel_id: int, owner_id: int, max_retries: int = 3,
    headers: dict | None = None,
) -> str:
    """Download then upload a video. Returns outcome: 'ok' | 'error' | 'cancelled'.

    The caller (the queue worker) is responsible for serialization. Status
    updates are sent by editing `status_msg`; final errors go to `owner_id`.
    """
    global _cancel_requested
    _cancel_requested = False
    _log(f"Download iniziato: {url} [{quality}]")

    loop = asyncio.get_running_loop()

    try:
        last_progress = 0.0
        last_log_progress = 0.0  # separate throttle for the log file

        def download_progress(downloaded_mb, total_mb, speed_mbps, eta) -> None:
            nonlocal last_progress, last_log_progress
            if _cancel_requested:
                raise CancelDownload("stop")
            now = time_module.time()
            if now - last_progress < 3:
                return
            last_progress = now
            pct = (downloaded_mb / total_mb * 100) if total_mb > 0 else 0
            text = f"⏳ **Download in corso...**\n▫️ {format_size(downloaded_mb)}"
            if total_mb > 0:
                text += f" / {format_size(total_mb)}"
            text += f"\n▫️ Velocità: {format_speed(speed_mbps)}"
            if eta and int(eta) > 0:
                text += f"\n▫️ Tempo rimanente: {format_eta(eta)}"
            text += f"\n▫️ {pct:.0f}%"
            # Edit the Telegram message only when not muted by FloodWait.
            if now >= _edit_muted_until:
                asyncio.run_coroutine_threadsafe(_safe_edit(status_msg, text), loop)
            # Always log progress to the file/console every 5s so the log
            # window shows the download is alive even when Telegram edits are muted.
            if now - last_log_progress >= 5:
                last_log_progress = now
                tot_str = f"/{total_mb:.0f}MB" if total_mb > 0 else ""
                eta_str = f" eta={int(eta)}s" if eta and int(eta) > 0 else ""
                _log(f"Progress: {downloaded_mb:.0f}MB{tot_str} {pct:.0f}% "
                     f"{format_speed(speed_mbps)}{eta_str}")

        filepath = None
        download_error = None

        for attempt in range(1, max_retries + 1):
            if _cancel_requested:
                cleanup_orphan_files()
                await _safe_edit(status_msg, "🛑 Download annullato.")
                return "cancelled"
            try:
                if attempt > 1:
                    await _safe_edit(status_msg, f"⏳ Download (tentativo {attempt}/{max_retries})...")
                filepath = await loop.run_in_executor(
                    None, download_video, url, quality, download_progress, 1,
                    headers,
                )
                _log(f"Download completato: {filepath}")
                break
            except CancelDownload:
                cleanup_orphan_files()
                await _safe_edit(status_msg, "🛑 Download annullato.")
                return "cancelled"
            except DownloadError as e:
                download_error = str(e)
                _log(f"Tentativo {attempt} fallito: {download_error}")
                if attempt < max_retries:
                    await asyncio.sleep(2 ** (attempt - 1))
            except Exception as e:
                if _cancel_requested:
                    cleanup_orphan_files()
                    await _safe_edit(status_msg, "🛑 Download annullato.")
                    return "cancelled"
                download_error = repr(e)
                _log(f"Errore download (tentativo {attempt}): {download_error}")
                cleanup_orphan_files()
                await _safe_edit(status_msg, f"❌ Download fallito: {e}")
                try:
                    _get_history().set_error(url, str(e), title)
                except Exception:
                    pass
                return "error"

        if filepath is None:
            cleanup_orphan_files()
            await _safe_edit(status_msg,
                f"❌ Download fallito dopo {max_retries} tentativi.\nErrore: {download_error}")
            try:
                _get_history().set_error(url, download_error or "download fallito", title)
            except Exception:
                pass
            return "error"

        file_size_bytes = os.path.getsize(filepath) if os.path.exists(filepath) else 0
        file_size_mb = file_size_bytes / (1024 * 1024)
        file_size_gb = file_size_mb / 1024

        if file_size_gb > 2:
            if os.path.exists(filepath):
                os.remove(filepath)
            await _safe_edit(status_msg,
                f"❌ File troppo grande ({file_size_gb:.1f} GB). Limite Telegram: 2GB")
            return "error"

        # ─── Upload phase: FastTelethon parallel upload ───
        caption = _build_caption(title, url)
        upload_success = False
        upload_error = None
        last_upload_progress = 0.0

        class _UpTracker:
            prev_bytes: int = 0
            prev_ts: float = 0.0

        tracker = _UpTracker()

        async def upload_progress(sent: int, total: int):
            nonlocal last_upload_progress
            if _cancel_requested:
                raise CancelDownload("stop")
            now = time_module.time()
            if now - last_upload_progress < 5:
                return
            if now < _edit_muted_until:
                return
            last_upload_progress = now
            pct = (sent / total * 100) if total > 0 else 0
            delta_bytes = sent - tracker.prev_bytes
            delta_t = now - tracker.prev_ts if tracker.prev_ts else 0
            speed_mbps = (delta_bytes / delta_t / 1024 / 1024) if delta_t > 0 and delta_bytes > 0 else 0
            remaining = total - sent
            eta = remaining / (delta_bytes / delta_t) if delta_bytes > 0 and delta_t > 0 else 0
            tracker.prev_bytes = sent
            tracker.prev_ts = now
            text = (
                f"✅ Download completato ({format_size(file_size_mb)})\n"
                f"📤 Upload {format_size(sent/(1024*1024))} / {format_size(total/(1024*1024))} · {pct:.0f}%"
            )
            if speed_mbps > 0:
                text += f"\n▫️ Velocità: {format_speed(speed_mbps)}"
            if eta > 0:
                text += f"\n▫️ Tempo rimanente: {format_eta(eta)}"
            await _safe_edit(status_msg, text)

        await _safe_edit(status_msg,
            f"✅ Download completato ({format_size(file_size_mb)})\n📤 Upload in corso (parallelo)...")

        # Probe real video metadata so Telegram shows it as a playable MP4
        # (with streaming + thumbnail) instead of a generic document.
        v_duration, v_w, v_h = await loop.run_in_executor(
            None, probe_video_metadata, filepath
        )

        for attempt in range(1, max_retries + 1):
            try:
                tracker.prev_ts = time_module.time()
                with open(filepath, "rb") as f:
                    uploaded = await upload_file(client, f, progress_callback=upload_progress)
                await client.send_file(
                    channel_id, file=uploaded, caption=caption,
                    attributes=[DocumentAttributeVideo(
                        duration=v_duration, w=v_w, h=v_h, supports_streaming=True
                    )],
                )
                upload_success = True
                _log("Upload completato con successo")
                break
            except CancelDownload:
                if os.path.exists(filepath):
                    try:
                        os.remove(filepath)
                    except Exception:
                        pass
                await _safe_edit(status_msg, "🛑 Upload annullato. File eliminato.")
                return "cancelled"
            except FloodWaitError as e:
                _log(f"FloodWait upload: {e.seconds}s")
                await asyncio.sleep(e.seconds)
            except Exception as e:
                if _cancel_requested:
                    if os.path.exists(filepath):
                        try:
                            os.remove(filepath)
                        except Exception:
                            pass
                    await _safe_edit(status_msg, "🛑 Upload annullato. File eliminato.")
                    return "cancelled"
                upload_error = repr(e)
                _log(f"Upload tentativo {attempt} fallito: {upload_error}")
                if attempt < max_retries:
                    await asyncio.sleep(2 ** (attempt - 1))

        if os.path.exists(filepath):
            try:
                os.remove(filepath)
            except Exception as e:
                _log(f"Cleanup fallito: {e!r}")

        if upload_success:
            await _safe_edit(status_msg, "✅ Video inviato con successo al canale!")
            try:
                _get_history().set_success(url, filepath, title)
            except Exception as e:
                _log(f"history save failed: {e!r}")
            return "ok"
        else:
            await _safe_edit(status_msg,
                f"❌ Upload fallito dopo {max_retries} tentativi.\nErrore: {upload_error}")
            try:
                _get_history().set_error(url, upload_error or "upload fallito", title)
            except Exception:
                pass
            return "error"

    finally:
        pass


# ─── Queue worker ───

async def _queue_worker(client, channel_id: int, owner_id: int) -> None:
    """Process the queue sequentially: peek -> download+upload -> remove.

    Uses peek()+remove() (NOT pop()): an item is removed only after the
    worker finishes handling it. If the bot crashes mid-download, the item
    remains in the queue and is reprocessed on next startup (yt-dlp resumes
    .part files). Removed regardless of outcome (ok/error/cancelled) — only
    a crash leaves it, by design.
    """
    global _current_item
    queue = _get_queue()
    _log("Queue worker avviato")
    while True:
        item = queue.peek()
        if item is None:
            _current_item = None
            await asyncio.sleep(2)  # coda vuota, polling leggero
            continue
        _current_item = item
        url = item["url"]
        quality = item.get("quality", "720")
        title = item.get("title", "Video")
        headers = item.get("headers") or None
        _log(f"Worker processa: {url} [{quality}]")
        try:
            status_msg = await client.send_message(
                owner_id, f"⏳ Avvio download: **{_escape_md(title)}** [{quality}]..."
            )
            await download_and_upload(
                client, status_msg, url, quality, title, channel_id, owner_id,
                headers=headers,
            )
        except Exception as e:
            _log(f"Worker errore inatteso su {url}: {e!r}")
        finally:
            # Always remove: the item has been handled (ok/error/cancelled).
            # If we crashed before reaching here, the item stays — that's the
            # crash-safe property; on restart it gets reprocessed.
            queue.remove(url)
            _current_item = None


async def start_queue_worker(client, channel_id: int, owner_id: int) -> None:
    """Called once at startup. Handles the resume prompt, then starts worker.

    If the queue has items from a previous run, asks the owner whether to
    resume. Only on "no" does it delete partial files and clear the queue
    (the conditional cleanup — destructive cleanup is deferred to user choice).
    On "yes" (or empty queue) the worker starts; yt-dlp resumes .part files.
    """
    global _resume_event, _resume_decision, _queue_worker_task
    queue = _get_queue()
    if queue.is_empty():
        # Truly orphan files (no queue items) -> safe to clean now.
        cleanup_orphan_files()
    else:
        lines = ["📥 Ci sono download in coda dal precedente avvio:\n"]
        for i, it in enumerate(queue.items, 1):
            lines.append(f"{i}. {_escape_md((it.get('title') or 'Sconosciuto')[:55])} [{it.get('quality','?')}]")
        lines.append("\nScrivi `si` per riprendere (i file parziali verranno continuati) "
                     "o `no` per annullare e pulire.")
        await client.send_message(owner_id, "\n".join(lines))
        _resume_event = asyncio.Event()
        _set_pending(owner_id, {"type": "confirm_resume"})
        await _resume_event.wait()
        decision = _resume_decision
        _resume_event = None
        _resume_decision = None
        if decision == "no":
            cleanup_orphan_files()
            queue.clear()
            await client.send_message(owner_id, "📭 Coda svuotata e file parziali rimossi.")
        else:
            await client.send_message(owner_id, "▶️ Riprendo la coda...")
    _queue_worker_task = asyncio.create_task(_queue_worker(client, channel_id, owner_id))


# ─── Upload existing file (no re-download) ───

async def _upload_existing(
    client, event, status_msg, filepath: str, title: str, channel_id: int, url: str = "", max_retries: int = 2,
) -> None:
    if not os.path.exists(filepath):
        await _safe_edit(status_msg, "❌ File non più presente. Rimanda il link.")
        return

    file_size_bytes = os.path.getsize(filepath)
    file_size_mb = file_size_bytes / (1024 * 1024)
    file_size_gb = file_size_mb / 1024
    if file_size_gb > 2:
        await _safe_edit(status_msg, f"❌ File troppo grande ({file_size_gb:.1f} GB).")
        return

    caption = _build_caption(title, url)
    upload_success = False
    upload_error = None
    last_progress = 0.0

    class _UpTracker:
        prev_bytes: int = 0
        prev_ts: float = 0.0
    tracker = _UpTracker()

    async def upload_progress(sent: int, total: int):
        nonlocal last_progress
        now = time_module.time()
        if now - last_progress < 5:
            return
        if now < _edit_muted_until:
            return
        last_progress = now
        pct = (sent / total * 100) if total > 0 else 0
        delta_bytes = sent - tracker.prev_bytes
        delta_t = now - tracker.prev_ts if tracker.prev_ts else 0
        speed_mbps = (delta_bytes / delta_t / 1024 / 1024) if delta_t > 0 and delta_bytes > 0 else 0
        remaining = total - sent
        eta = remaining / (delta_bytes / delta_t) if delta_bytes > 0 and delta_t > 0 else 0
        tracker.prev_bytes = sent
        tracker.prev_ts = now
        text = f"📤 Upload {format_size(sent/(1024*1024))} / {format_size(total/(1024*1024))} · {pct:.0f}%"
        if speed_mbps > 0:
            text += f"\n▫️ Velocità: {format_speed(speed_mbps)}"
        if eta > 0:
            text += f"\n▫️ Tempo rimanente: {format_eta(eta)}"
        await _safe_edit(status_msg, text)

    await _safe_edit(status_msg, f"📤 Upload in corso: **{_escape_md(title)}** ({format_size(file_size_mb)})...")

    # Probe real video metadata so Telegram shows it as a playable MP4
    # (with streaming + thumbnail) instead of a generic document.
    loop = asyncio.get_running_loop()
    v_duration, v_w, v_h = await loop.run_in_executor(None, probe_video_metadata, filepath)

    for attempt in range(1, max_retries + 1):
        try:
            tracker.prev_ts = time_module.time()
            with open(filepath, "rb") as f:
                uploaded = await upload_file(client, f, progress_callback=upload_progress)
            await client.send_file(
                channel_id, file=uploaded, caption=caption,
                attributes=[DocumentAttributeVideo(
                    duration=v_duration, w=v_w, h=v_h, supports_streaming=True
                )],
            )
            upload_success = True
            _log("Upload esistente completato")
            break
        except FloodWaitError as e:
            _log(f"FloodWait: {e.seconds}s")
            await asyncio.sleep(e.seconds)
        except Exception as e:
            upload_error = repr(e)
            _log(f"Upload tentativo {attempt} fallito: {upload_error}")
            if attempt < max_retries:
                await asyncio.sleep(2 ** (attempt - 1))

    if upload_success:
        await _safe_edit(status_msg, "✅ Video inviato con successo al canale!")
        try:
            _get_history().set_success(url, filepath, title)
        except Exception as e:
            _log(f"history save failed: {e!r}")
    else:
        await _safe_edit(status_msg,
            f"❌ Upload fallito dopo {max_retries} tentativi.\nErrore: {upload_error}")
        try:
            _get_history().set_error(url, upload_error or "upload fallito", title)
        except Exception:
            pass


# ─── Process a new link ───

async def _process_link(client, event, url: str, channel_id: int) -> None:
    user_id = event.sender_id

    # vidxgo / vidplay player URLs only work embedded inside a parent page
    # (altadefinizione, streamingcommunity). Direct navigation returns 403/404.
    # Tell the user to send the movie/series page URL instead.
    _lower = url.lower()
    if "vidxgo.co" in _lower or "vidplay." in _lower or "vidplaylink" in _lower:
        _log(f"Link player diretto non supportato: {url}")
        await _safe_reply(event,
            "❌ Questo è un link del **player** (vidxgo/vidplay) che funziona solo "
            "incorporato nella pagina del sito.\n\n"
            "📎 Invia invece il link della **pagina del film/serie** su "
            "altadefinizione o streamingcommunity, e il bot troverà il video "
            "automaticamente.")
        return

    # Streaming-site extractors (Playwright) take priority over yt-dlp for
    # Cloudflare-protected sites (streamingcommunity, altadefinizione) that
    # yt-dlp can't handle. They return a direct m3u8 URL + the anti-leech
    # headers the CDN requires.
    extractor = get_extractor(url)
    if extractor is not None:
        status_msg = await _safe_reply(event, "🌐 Estrazione video via browser...")
        try:
            loop = asyncio.get_running_loop()
            info = await extractor.extract(url)
            _log(f"Extractor ok: {info.url}")
            await _safe_delete(status_msg)
            await _send_quality_menu(event, user_id, info.url, info.title, info.headers)
        except Exception as e:
            _log(f"Extractor fallito: {e!r}")
            await _safe_edit(status_msg, f"❌ Estrazione fallita: {e}")
        return

    entry = _get_history().get(url)
    if entry:
        status = entry.get("status")
        title = entry.get("title", "")
        ts = entry.get("ts", 0)
        date_str = time_module.strftime("%d/%m/%Y", time_module.localtime(ts)) if ts else "?"
        if status == "ok":
            filepath = entry.get("filepath", "")
            if os.path.exists(filepath):
                await _safe_reply(event,
                    f"📂 **{_escape_md(title)}** già scaricato (uploadato il {date_str}).\n"
                    f"Scrivi `si` per ricaricarlo, o `no` per annullare.")
                _set_pending(user_id, {"type": "confirm_retry", "action": "reupload",
                    "filepath": filepath, "title": title, "url": url})
                return
            else:
                await _safe_reply(event,
                    f"📂 **{_escape_md(title)}** è già stato uploadato il {date_str}.\n"
                    f"Scrivi `si` per riscaricarlo, o `no` per annullare.")
                _set_pending(user_id, {"type": "confirm_retry", "action": "retry_download",
                    "url": url, "title": title})
                return
        elif status == "error":
            err = entry.get("error", "errore sconosciuto")
            await _safe_reply(event,
                f"⚠️ Questo link ha dato errore: _{err}_\n"
                f"Scrivi `si` per riprovare o `no` per annullare.")
            _set_pending(user_id, {"type": "confirm_retry", "action": "retry_download",
                "url": url, "title": title})
            return

    status_msg = await _safe_reply(event, "🔍 Analisi del link in attesa...")
    try:
        async with _extract_lock:
            await _safe_edit(status_msg, "🔍 Analisi del link in corso...")
            loop = asyncio.get_running_loop()
            info = await loop.run_in_executor(None, extract_info, url)
        if isinstance(info, list):
            if not info:
                await _safe_edit(status_msg, "❌ Playlist vuota.")
                return
            await _safe_delete(status_msg)
            await _send_playlist_menu(event, user_id, info, url, page=0)
        else:
            title = info.get("title", "Sconosciuto")
            await _safe_delete(status_msg)
            await _send_quality_menu(event, user_id, url, title)
    except VideoUnavailableError as e:
        # yt-dlp segnala il contenuto come non disponibile/paywall. Se il sito
        # è nella lista universale, proviamo comunque col browser normale
        # (accesso standard, nessun bypass): se la sorgente serve davvero il
        # video a un browser anonimo, la risorsa è disponibile -> si scarica;
        # altrimenti messaggio chiaro e stop.
        _log(f"VideoUnavailable: {e}")
        fallback = get_universal_fallback(url)
        if fallback is not None:
            _log("yt-dlp segnala non disponibile: verifico comunque col browser (accesso standard)")
            await _safe_edit(status_msg, "🌐 Verifica disponibilità via browser...")
            try:
                finfo = await fallback.extract(url)
                _log("Risorsa servita al browser: %s", finfo.url[:80])
                await _safe_delete(status_msg)
                await _send_quality_menu(event, user_id, finfo.url, finfo.title, finfo.headers)
            except Exception:
                _log("Fallback universale: risorsa non servita dal sito")
                await _safe_edit(status_msg, f"🔒 {e}")
            return
        await _safe_edit(status_msg, f"🔒 {e}")
    except ExtractError as e:
        _log(f"ExtractError: {e}")
        # yt-dlp non ce l'ha fatta per motivi TECNICI: prova il fallback
        # universale Playwright (siti free-tube con extractor rotto/obsoleto).
        fallback = get_universal_fallback(url)
        if fallback is not None:
            _log("yt-dlp fallito (%s): provo extractor universale", str(e)[:80])
            await _safe_edit(status_msg, "🌐 Estrazione via browser (fallback)...")
            try:
                finfo = await fallback.extract(url)
                _log("Fallback universale ok: %s", finfo.url[:80])
                await _safe_delete(status_msg)
                await _send_quality_menu(event, user_id, finfo.url, finfo.title, finfo.headers)
            except Exception as e2:
                _log(f"Fallback universale fallito: {e2!r}")
                await _safe_edit(status_msg, f"❌ {e}")
            return
        await _safe_edit(status_msg, f"❌ {e}")
    except Exception as e:
        _log(f"Errore analisi: {e!r}")
        await _safe_edit(status_msg, f"❌ Errore: {e}")


# ─── Handle selection ───

async def _handle_selection(client, event, user_id: int, text: str, pending: dict, channel_id: int) -> None:
    text_lower = text.lower().strip()

    if pending["type"] == "confirm_resume":
        global _resume_decision
        _clear_pending(user_id)
        _resume_decision = "yes" if text_lower in ("si", "sì", "yes", "y") else "no"
        if _resume_event is not None:
            _resume_event.set()
        return

    if pending["type"] == "confirm_clean":
        _clear_pending(user_id)
        if text_lower in ("si", "sì", "yes", "y"):
            queue = _get_queue()
            n = queue.clear()
            await _safe_reply(event, f"🧹 Coda svuotata ({n} item rimossi).")
        else:
            await _safe_reply(event, "👌 Operazione annullata.")
        return

    if pending["type"] == "confirm_retry":
        if text_lower in ("si", "sì", "yes", "y"):
            action = pending["action"]
            if action == "reupload":
                filepath = pending["filepath"]
                title = pending["title"]
                url = pending.get("url", "")
                _clear_pending(user_id)
                # Remove history entry so we don't hit confirm_retry loop on next link
                try:
                    _get_history().remove(url)
                except Exception:
                    pass
                status_msg = await _safe_reply(event, f"📤 Invio file esistente: **{_escape_md(title)}**...")
                await _upload_existing(client, event, status_msg, filepath, title, channel_id, url)
            elif action == "retry_download":
                url = pending["url"]
                _clear_pending(user_id)
                # Remove history entry so _process_link doesn't loop again
                try:
                    _get_history().remove(url)
                except Exception:
                    pass
                await _process_link(client, event, url, channel_id)
            else:
                _clear_pending(user_id)
        else:
            _clear_pending(user_id)
            await _safe_reply(event, "👌 Annullato.")
        return

    if pending["type"] == "playlist":
        videos = pending["videos"]
        page_size = 10
        total_pages = (len(videos) + page_size - 1) // page_size or 1

        if text_lower in ("next", ">", "avanti"):
            new_page = min(pending["page"] + 1, total_pages - 1)
            await _send_playlist_menu(event, user_id, videos, pending["url"], page=new_page)
            return
        if text_lower in ("prev", "<", "indietro"):
            new_page = max(pending["page"] - 1, 0)
            await _send_playlist_menu(event, user_id, videos, pending["url"], page=new_page)
            return

        try:
            num = int(text_lower)
        except ValueError:
            await _safe_reply(event, "❌ Scrivi il NUMERO del video (o `next`/`prev`).")
            return
        if not (1 <= num <= len(videos)):
            await _safe_reply(event, f"❌ Numero tra 1 e {len(videos)}.")
            return

        video = videos[num - 1]
        video_url = video.get("webpage_url") or video.get("url", "")
        if not video_url:
            await _safe_reply(event, "❌ URL non disponibile.")
            return

        status_msg = await _safe_reply(event, "🔍 Analisi del video...")
        try:
            loop = asyncio.get_running_loop()
            info = await loop.run_in_executor(None, extract_info, video_url)
            if isinstance(info, list):
                info = info[0] if info else {}
            title = info.get("title", "Sconosciuto")
            if status_msg:
                await _safe_delete(status_msg)
            await _send_quality_menu(event, user_id, video_url, title)
        except ExtractError as e:
            if status_msg:
                await _safe_edit(status_msg, f"❌ {e}")
        except Exception as e:
            _log(f"Errore: {e!r}")
            if status_msg:
                await _safe_edit(status_msg, f"❌ Errore: {e}")
        return

    if pending["type"] == "quality":
        choice = QUALITY_CHOICES.get(text_lower)
        if text_lower in ("360", "720", "1080", "max"):
            label = "MAX" if text_lower == "max" else f"{text_lower}p"
            quality = text_lower
        elif choice:
            label, quality = choice
        else:
            await _safe_reply(event, "❌ Scrivi `1`, `2`, `3` o `4`.")
            return

        url = pending["url"]
        title = pending["title"]
        headers = pending.get("headers", {})
        _clear_pending(user_id)
        _log(f"Qualità scelta da {user_id}: {quality}")
        queue = _get_queue()
        pos = queue.add(url, quality, title, headers)
        if _current_item is None:
            # Niente in lavorazione: questo parte subito (o è il primo dopo cleanup).
            if pos == 1:
                await _safe_reply(event, f"⏳ Avvio download in qualità **{label}**...")
            else:
                await _safe_reply(event,
                    f"📥 Aggiunto alla coda (posizione {pos}). Avvio a breve.")
        else:
            # Un download è in corso: questo verrà processato dopo.
            await _safe_reply(event,
                f"📥 Aggiunto alla coda (posizione {pos}). "
                f"Verrà scaricato al termine di quello in corso.")
        return


# ─── Message handler ───

async def on_message(client, event, whitelist: Whitelist, channel_id: int, owner_id: int) -> None:
    user_id = event.sender_id
    if user_id is None:
        return
    if user_id != owner_id and not whitelist.is_authorized(user_id):
        return

    text = (event.message.text or "").strip()
    url = extract_url(text)

    if url:
        _clear_pending(user_id)
        _log(f"Nuovo link da {user_id}: {url}")
        await _process_link(client, event, url, channel_id)
        return

    pending = _get_pending(user_id)
    if pending:
        await _handle_selection(client, event, user_id, text, pending, channel_id)
        return

    _log(f"Messaggio ignorato da {user_id}: {text[:40]!r}")


# ─── Admin commands ───

async def cmd_adduser(client, event, whitelist: Whitelist, owner_id: int) -> None:
    if event.sender_id != owner_id:
        return
    parts = (event.message.text or "").split()
    if len(parts) < 2:
        await _safe_reply(event, "❌ Uso: `/adduser @username` o `/adduser 123456789`")
        return
    target = parts[1]
    if target.startswith("@"):
        try:
            entity = await client.get_entity(target)
            user_id = entity.id
            display_name = f"@{entity.username}" if getattr(entity, "username", None) else getattr(entity, "first_name", str(user_id))
        except Exception as e:
            _log(f"get_entity fallito: {e!r}")
            await _safe_reply(event, f"❌ Impossibile trovare `{target}`.")
            return
    else:
        try:
            user_id = int(target)
            display_name = str(user_id)
        except ValueError:
            await _safe_reply(event, "❌ Formato non valido.")
            return
    if whitelist.is_authorized(user_id):
        await _safe_reply(event, f"ℹ️ {display_name} è già autorizzato.")
        return
    whitelist.add(user_id, username=target, added_by=str(event.sender_id))
    await _safe_reply(event, f"✅ {display_name} aggiunto alla whitelist.")


async def cmd_removeuser(client, event, whitelist: Whitelist, owner_id: int) -> None:
    if event.sender_id != owner_id:
        return
    parts = (event.message.text or "").split()
    if len(parts) < 2:
        await _safe_reply(event, "❌ Uso: `/removeuser @username`")
        return
    target = parts[1]
    if target.startswith("@"):
        user_id = None
        for uid, udata in whitelist.get_all().items():
            if udata.get("username", "").lower() == target.lower():
                user_id = uid
                break
        if user_id is None:
            await _safe_reply(event, f"❌ {target} non trovato.")
            return
    else:
        try:
            user_id = int(target)
        except ValueError:
            await _safe_reply(event, "❌ Formato non valido.")
            return
    if whitelist.remove(user_id):
        await _safe_reply(event, f"✅ {target} rimosso.")
    else:
        await _safe_reply(event, f"❌ {target} non era nella whitelist.")


async def cmd_users(client, event, whitelist: Whitelist, owner_id: int) -> None:
    if event.sender_id != owner_id:
        return
    users = whitelist.get_all()
    if not users:
        await _safe_reply(event, "📭 Nessun utente nella whitelist.")
        return
    lines = []
    for uid, udata in users.items():
        username = udata.get("username", str(uid))
        added_at = udata.get("added_at", "?")[:10]
        lines.append(f"• `{uid}` — {username} (dal {added_at})")
    await _safe_reply(event, f"**📋 Autorizzati ({len(users)}):**\n" + "\n".join(lines))


async def cmd_channel(client, event, channel_id: int) -> None:
    try:
        entity = await client.get_entity(channel_id)
        name = getattr(entity, "title", str(channel_id))
        await _safe_reply(event, f"📺 Canale: **{name}** (`{channel_id}`)")
    except Exception:
        await _safe_reply(event, f"📺 Canale: `{channel_id}`")


async def cmd_queue(client, event, owner_id: int) -> None:
    if event.sender_id != owner_id:
        return
    queue = _get_queue()
    lines = []
    if _current_item is not None:
        lines.append(f"🎬 In corso: **{_escape_md((_current_item.get('title') or '')[:55])}** [{_current_item.get('quality','?')}]")
    else:
        lines.append("🎬 Nessun download in corso.")
    items = queue.items
    lines.append(f"\n📋 In coda ({len(items)}):")
    if not items:
        lines.append("_(vuota)_")
    else:
        for i, it in enumerate(items, 1):
            lines.append(f"{i}. {_escape_md((it.get('title') or 'Sconosciuto')[:55])} [{it.get('quality','?')}]")
    await _safe_reply(event, "\n".join(lines))


async def cmd_now(client, event, owner_id: int) -> None:
    if event.sender_id != owner_id:
        return
    parts = (event.message.text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await _safe_reply(event, "❌ Uso: `/now <url>`")
        return
    url = parts[1].strip()
    queue = _get_queue()
    if queue.move_front(url):
        await _safe_reply(event, f"✅ Spostato in cima alla coda: `{url}`")
    else:
        await _safe_reply(event, f"❌ URL non presente in coda: `{url}`")


async def cmd_clean(client, event, owner_id: int) -> None:
    if event.sender_id != owner_id:
        return
    queue = _get_queue()
    if queue.is_empty() and _current_item is None:
        await _safe_reply(event, "📭 La coda è già vuota.")
        return
    n = len(queue.items)
    await _safe_reply(event, f"🧹 Vuoi svuotare la coda ({n} item in attesa)? Scrivi `si` o `no`.")
    _set_pending(event.sender_id, {"type": "confirm_clean"})


async def cmd_status(client, event):
    queue = _get_queue()
    pending = len(queue.items)
    if _current_item is not None:
        line = f"📊 In corso: **{_escape_md((_current_item.get('title') or '')[:55])}**"
    else:
        line = "📊 Nessun download in corso."
    line += f"\n📋 In coda: {pending}"
    await _safe_reply(event, line)


async def cmd_stop(client, event, owner_id: int) -> None:
    global _cancel_requested
    if event.sender_id != owner_id:
        return
    if _current_item is not None:
        _cancel_requested = True
        _log("Stop richiesto dall'utente")
        replied = await _safe_reply(event, "🛑 Interruzione richiesta...")
        if replied is None:
            wait = max(0, _edit_muted_until - time_module.time())
            asyncio.create_task(_delayed_reply(event, wait, "🛑 Stop ricevuto."))
    else:
        removed = cleanup_orphan_files()
        await _safe_reply(event, f"📭 Nessun download. Puliti {len(removed)} file.")


async def _delayed_reply(event, wait: float, text: str) -> None:
    if wait > 0:
        _log(f"Conferma posticipata di {wait:.0f}s")
        await asyncio.sleep(wait)
    try:
        await event.reply(text)
    except Exception as e:
        _log(f"Conferma ritardata fallita: {e!r}")


# ─── Handler registration ───

def register_handlers(client, whitelist: Whitelist, channel_id: int, owner_id: int) -> None:
    @client.on(events.NewMessage(incoming=True))
    async def _on_message(event):
        if not event.is_private:
            return
        text = event.message.text or ""
        if event.sender_id == owner_id:
            if text.startswith("/adduser"):
                await cmd_adduser(client, event, whitelist, owner_id)
                return
            elif text.startswith("/removeuser"):
                await cmd_removeuser(client, event, whitelist, owner_id)
                return
            elif text.startswith("/users"):
                await cmd_users(client, event, whitelist, owner_id)
                return
            elif text.startswith("/channel"):
                await cmd_channel(client, event, channel_id)
                return
            elif text.startswith("/status"):
                await cmd_status(client, event)
                return
            elif text.startswith("/stop"):
                await cmd_stop(client, event, owner_id)
                return
            elif text.startswith("/queue"):
                await cmd_queue(client, event, owner_id)
                return
            elif text.startswith("/now"):
                await cmd_now(client, event, owner_id)
                return
            elif text.startswith("/clean"):
                await cmd_clean(client, event, owner_id)
                return
        await on_message(client, event, whitelist, channel_id, owner_id)
