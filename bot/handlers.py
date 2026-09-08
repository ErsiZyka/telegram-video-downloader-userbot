"""Message handlers for the video downloader userbot (Telethon + FastTelethon).

Userbot interaction model: user sends a link -> bot shows a numbered menu ->
user types the number -> bot acts. State is tracked per-user.
Upload uses FastTelethon for parallel TCP connections (10-20 MB/s).
"""

import asyncio
import os
import re
import time as time_module
from urllib.parse import urlparse

from telethon import events
from telethon.errors import FloodWaitError
from telethon.tl.types import (
    Document,
    DocumentAttributeVideo,
    MessageMediaDocument,
    MessageMediaPhoto,
    PeerChannel,
)

from bot import progress as _progress
from bot.downloader import (
    CancelDownload,
    DownloadError,
    ExtractError,
    VideoUnavailableError,
    _is_blocked_error,
    cleanup_orphan_files,
    download_video,
    extract_info,
    format_eta,
    format_size,
    format_speed,
    get_max_file_size_bytes,
    is_over_limit,
    probe_video_metadata,
)
from bot.extractors import get_extractor, get_universal_fallback
from bot.fasttelethon import download_file, upload_file
from bot.history import DownloadHistory
from bot.queue import DownloadQueue
from bot.whitelist import Whitelist

URL_REGEX = re.compile(r"(https?://[^\s]+)")

# t.me post links: https://t.me/<username>/<msg_id> (public chat/channel)
# or https://t.me/c/<id>/<msg_id> (private channel, numeric id without -100)
TG_LINK_REGEX = re.compile(
    r"^(?:https?://)?t\.me/(?:(?P<prefix>c)/)?(?P<entity>[A-Za-z0-9_]+)/(?P<msg>\d+)",
    re.IGNORECASE,
)

# ─── State ───
_cancel_requested = False
_edit_muted_until = 0.0

_pending: dict[int, dict] = {}
PENDING_TIMEOUT = 600

_history: DownloadHistory | None = None

_queue: DownloadQueue | None = None
_current_item: dict | None = None
_progress_state: dict | None = (
    None  # {phase, title, pct, received, total, speed, eta, ts}
)
_queue_worker_task: asyncio.Task | None = None
_resume_event: asyncio.Event | None = None
_resume_decision: str | None = None  # "yes" | "no"
_extract_lock = asyncio.Lock()


def _set_progress(
    phase: str,
    title: str | None,
    pct: float,
    received: float,
    total: float,
    speed: float = 0.0,
    eta: float = 0.0,
    job: str | None = None,
) -> None:
    """Aggiorna lo stato live, anche quando il callback gira in un thread.

    ``job=None`` usa il job corrente del worker; lo stato resta consultabile
    per-job (API/sito) oltre che come singolo globale legacy (/status).
    """
    global _progress_state
    _progress_state = {
        "phase": phase,
        "title": title,
        "pct": pct,
        "received": received,
        "total": total,
        "speed": speed,
        "eta": eta,
        "ts": time_module.time(),
    }
    _progress.set_progress(
        job, phase, title, pct, received, total, speed, eta
    )


def _clear_progress(job: str | None = None) -> None:
    """Azzera lo stato quando l'item termina (legacy + per-job)."""
    global _progress_state
    _progress_state = None
    _progress.clear_progress(job)


QUALITY_CHOICES = {
    "1": ("360p", "360"),
    "2": ("720p", "720"),
    "3": ("1080p", "1080"),
    "4": ("MAX", "max"),
}


class SlowUploadError(Exception):
    pass


def _job_cancelled(url: str | None) -> bool:
    """True se /stop globale o un cancel per-job riguarda questo URL."""
    if _cancel_requested:
        return True
    return _progress.is_cancelled(url)


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
        return


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
    match = URL_REGEX.search(text)
    if not match:
        return None
    url = match.group(1)
    # Strip trailing punctuation that often gets pasted along with the URL
    # (quotes, commas, periods, closing parens/brackets, colons, semicolons).
    url = url.rstrip("\"'.,);:]")
    return url


def extract_all_urls(text: str) -> list[str]:
    """Extract ALL http(s) URLs from a message, in order, deduplicated.

    Used for batch downloads: multiple links in one message are detected
    automatically and queued one by one.
    """
    if not text:
        return []
    urls: list[str] = []
    for u in URL_REGEX.findall(text):
        u = u.rstrip("\"'.,);:]")
        if u not in urls:
            urls.append(u)
    return urls


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
    if msg is None:
        return
    try:
        await msg.delete()
    except Exception as e:
        _log(f"delete fallito: {e!r}")


# ─── Menu senders ───


async def _send_quality_menu(
    event, user_id: int, url: str, title: str, headers: dict | None = None
) -> None:
    menu = await _safe_reply(
        event,
        f"🎬 **{_escape_md(title)}**\n\n"
        f"Scrivi qui il NUMERO della qualità:\n"
        f"1️⃣ 360p\n2️⃣ 720p\n3️⃣ 1080p\n🔥 4️⃣ MAX",
    )
    if menu is None:
        _log("Menu qualità non inviato (FloodWait?)")
        return
    _set_pending(
        user_id,
        {"type": "quality", "url": url, "title": title, "headers": headers or {}},
    )
    _log(f"Menu qualità inviato a {user_id}")


async def _send_playlist_menu(
    event, user_id: int, videos: list, url: str, page: int = 0
) -> None:
    total = len(videos)
    page_size = 10
    total_pages = (total + page_size - 1) // page_size or 1
    start = page * page_size
    page_videos = videos[start : start + page_size]

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
    _set_pending(
        user_id, {"type": "playlist", "videos": videos, "url": url, "page": page}
    )
    _log(f"Menu playlist inviato a {user_id} ({total} video)")


# ─── Core: download_and_upload (FastTelethon parallel upload) ───


async def download_and_upload(
    client,
    status_msg,
    url: str,
    quality: str,
    title: str,
    channel_id: int,
    owner_id: int,
    max_retries: int = 3,
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
            if _job_cancelled(url):
                raise CancelDownload("stop")
            now = time_module.time()
            pct = (downloaded_mb / total_mb * 100) if total_mb > 0 else 0
            _set_progress(
                "download",
                title,
                pct,
                downloaded_mb,
                total_mb,
                speed_mbps,
                float(eta or 0),
            )
            if now - last_progress < 3:
                return
            last_progress = now
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
                _log(
                    f"Progress: {downloaded_mb:.0f}MB{tot_str} {pct:.0f}% "
                    f"{format_speed(speed_mbps)}{eta_str}"
                )

        filepath = None
        download_error = None

        # Un solo tentativo esterno: i retry con backoff vivono DENTRO
        # download_video, dove lo stato di yt-dlp (boost frammenti, header
        # browser, fallback di formato) si conserva tra un tentativo e
        # l'altro. Prima c'erano due livelli (esterno x3 con inner=1) e il
        # boost slow-start andava perso a ogni tentativo.
        _progress.set_current_job(url)
        if _job_cancelled(url):
            cleanup_orphan_files()
            await _safe_edit(status_msg, "🛑 Download annullato.")
            return "cancelled"
        try:
            filepath = await loop.run_in_executor(
                None,
                download_video,
                url,
                quality,
                download_progress,
                max_retries,
                headers,
                title,
            )
            _log(f"Download completato: {filepath}")
        except CancelDownload:
            cleanup_orphan_files()
            await _safe_edit(status_msg, "🛑 Download annullato.")
            return "cancelled"
        except DownloadError as e:
            download_error = str(e)
            _log(f"Download fallito dopo {max_retries} tentativi: {download_error}")
        except Exception as e:
            if _job_cancelled(url):
                cleanup_orphan_files()
                await _safe_edit(status_msg, "🛑 Download annullato.")
                return "cancelled"
            download_error = repr(e)
            _log(f"Errore download: {download_error}")
            cleanup_orphan_files()
            await _safe_edit(status_msg, f"❌ Download fallito: {e}")
            try:
                _get_history().set_error(url, str(e), title)
            except Exception:
                _log("history save fallito (ignorato)")
            return "error"

        # Il CDN rifiuta il download (403/forbidden) pur avendo yt-dlp
        # estratto correttamente l'URL: riprova con il fallback browser,
        # che ottiene un URL fresco + gli header anti-leech richiesti dal sito.
        if filepath is None and _is_blocked_error(download_error or ""):
                fallback = get_universal_fallback(url)
                if fallback is not None:
                    _log(
                        f"Download bloccato dal CDN ({(download_error or '')[:90]}): riestraggio via browser..."
                    )
                    await _safe_edit(
                        status_msg,
                        "🌐 CDN blocca il download: riestrazione via browser...",
                    )
                    try:
                        finfo = await fallback.extract(url)
                        _log(f"Fallback browser ok: {finfo.url[:80]}")
                        filepath = await loop.run_in_executor(
                            None,
                            download_video,
                            finfo.url,
                            quality,
                            download_progress,
                            1,
                            finfo.headers,
                            title,
                        )
                        _log(f"Download completato dopo fallback browser: {filepath}")
                    except CancelDownload:
                        cleanup_orphan_files()
                        await _safe_edit(status_msg, "🛑 Download annullato.")
                        return "cancelled"
                    except Exception as e3:
                        _log(f"Fallback browser fallito: {e3!r}")
                        filepath = None

        if filepath is None:
            cleanup_orphan_files()
            await _safe_edit(
                status_msg,
                f"❌ Download fallito dopo {max_retries} tentativi.\nErrore: {download_error}",
            )
            try:
                _get_history().set_error(
                    url, download_error or "download fallito", title
                )
            except Exception:
                _log("history save fallito (ignorato)")
            return "error"

        file_size_bytes = os.path.getsize(filepath) if os.path.exists(filepath) else 0
        file_size_mb = file_size_bytes / (1024 * 1024)

        if is_over_limit(file_size_bytes):
            try:
                if os.path.exists(filepath):
                    os.remove(filepath)
            except OSError as e:
                _log(f"cleanup file grande fallito: {e!r}")
            await _safe_edit(
                status_msg,
                f"❌ File troppo grande ({file_size_mb / 1024:.1f} GB). "
                f"Limite Telegram: {get_max_file_size_bytes() / 1024**3:g}GB",
            )
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
            if _job_cancelled(url):
                raise CancelDownload("stop")
            now = time_module.time()
            pct = (sent / total * 100) if total > 0 else 0
            delta_bytes = sent - tracker.prev_bytes
            delta_t = now - tracker.prev_ts if tracker.prev_ts else 0
            speed_mbps = (
                (delta_bytes / delta_t / 1024 / 1024)
                if delta_t > 0 and delta_bytes > 0
                else 0
            )
            remaining = total - sent
            eta = (
                remaining / (delta_bytes / delta_t)
                if delta_bytes > 0 and delta_t > 0
                else 0
            )
            _set_progress(
                "upload",
                title,
                pct,
                sent / (1024 * 1024),
                total / (1024 * 1024),
                speed_mbps,
                eta,
            )
            if now - last_upload_progress < 5:
                return
            if now < _edit_muted_until:
                return
            last_upload_progress = now
            tracker.prev_bytes = sent
            tracker.prev_ts = now
            text = (
                f"✅ Download completato ({format_size(file_size_mb)})\n"
                f"📤 Upload {format_size(sent / (1024 * 1024))} / {format_size(total / (1024 * 1024))} · {pct:.0f}%"
            )
            if speed_mbps > 0:
                text += f"\n▫️ Velocità: {format_speed(speed_mbps)}"
            if eta > 0:
                text += f"\n▫️ Tempo rimanente: {format_eta(eta)}"
            await _safe_edit(status_msg, text)

        await _safe_edit(
            status_msg,
            f"✅ Download completato ({format_size(file_size_mb)})\n📤 Upload in corso (parallelo)...",
        )

        # Probe real video metadata so Telegram shows it as a playable MP4
        # (with streaming + thumbnail) instead of a generic document.
        v_duration, v_w, v_h = await loop.run_in_executor(
            None, probe_video_metadata, filepath
        )

        for attempt in range(1, max_retries + 1):
            try:
                tracker.prev_ts = time_module.time()
                with open(filepath, "rb") as f:
                    uploaded = await upload_file(
                        client, f, progress_callback=upload_progress
                    )
                await client.send_file(
                    channel_id,
                    file=uploaded,
                    caption=caption,
                    attributes=[
                        DocumentAttributeVideo(
                            duration=v_duration, w=v_w, h=v_h, supports_streaming=True
                        )
                    ],
                )
                upload_success = True
                _log("Upload completato con successo")
                break
            except CancelDownload:
                if os.path.exists(filepath):
                    try:
                        os.remove(filepath)
                    except Exception:
                        _log("cleanup file annullato fallito (ignorato)")
                await _safe_edit(status_msg, "🛑 Upload annullato. File eliminato.")
                return "cancelled"
            except FloodWaitError as e:
                _log(f"FloodWait upload: {e.seconds}s")
                await _safe_edit(
                    status_msg,
                    f"⏳ Telegram mi ha limitato l'upload per ~{max(1, round(e.seconds / 60))} min"
                    f" ({e.seconds}s). Attendo e riprovo in automatico...",
                )
                # Attesa cancellabile: /stop interrompe subito invece di
                # lasciare il bot sordo per minuti (era causa di "sembra rotto").
                waited = 0
                while waited < e.seconds:
                    if _job_cancelled(url):
                        _log("FloodWait upload interrotto da /stop")
                        break
                    step = min(5, e.seconds - waited)
                    await asyncio.sleep(step)
                    waited += step
            except Exception as e:
                if _job_cancelled(url):
                    if os.path.exists(filepath):
                        try:
                            os.remove(filepath)
                        except Exception:
                            _log("cleanup file annullato fallito (ignorato)")
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
            await _safe_edit(
                status_msg,
                f"❌ Upload fallito dopo {max_retries} tentativi.\nErrore: {upload_error}",
            )
            try:
                _get_history().set_error(url, upload_error or "upload fallito", title)
            except Exception:
                _log("history save fallito (ignorato)")
            return "error"

    finally:
        _clear_progress()


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
        _progress.set_current_job(url)
        _set_progress("download", title, 0.0, 0.0, 0.0)
        headers = item.get("headers") or None
        _log(f"Worker processa: {url} [{quality}]")
        try:
            try:
                status_msg = await client.send_message(
                    owner_id,
                    f"⏳ Avvio download: **{_escape_md(title)}** [{quality}]...",
                )
            except FloodWaitError as e:
                # Rate-limit Telegram sul solo messaggio di stato: attendere è
                # obbligatorio, ma l'item NON deve fallire per questo. Attendi,
                # riprova una volta e, se ancora bloccato, prosegui senza
                # messaggio di stato (gli aggiornamenti restano nel log).
                _log(
                    f"FloodWait {e.seconds}s su 'Avvio download' per {url}: attendo..."
                )
                await asyncio.sleep(min(e.seconds + 3, 600))
                try:
                    status_msg = await client.send_message(
                        owner_id,
                        f"⏳ Avvio download: **{_escape_md(title)}** [{quality}]...",
                    )
                except FloodWaitError as e2:
                    _log(
                        f"FloodWait persistente ({e2.seconds}s): proseguo senza status message"
                    )
                    status_msg = None
            kind = item.get("kind", "web")
            if kind == "saved":
                await download_and_upload_saved(
                    client, status_msg, item, channel_id, owner_id
                )
            else:
                await download_and_upload(
                    client,
                    status_msg,
                    url,
                    quality,
                    title,
                    channel_id,
                    owner_id,
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
            _progress.clear_cancel(url)
            _progress.set_current_job(None)
            _clear_progress()


async def start_queue_worker(client, channel_id: int, owner_id: int) -> None:
    """Called once at startup. Starts the queue worker IMMEDIATELY.

    Niente prompt di conferma: il worker parte sempre subito. Se ci sono
    item dal precedente avvio li riprende (yt-dlp continua i file .part,
    gli item kind="saved" riscaricano dai Messaggi Salvati). Il vecchio
    prompt "si/no" era causa di blocchi infiniti quando nessuno rispondeva
    — il worker restava fermo e la coda si accumulava.
    """
    global _queue_worker_task
    queue = _get_queue()
    if queue.is_empty():
        # Truly orphan files (no queue items) -> safe to clean now.
        cleanup_orphan_files()
    else:
        await client.send_message(
            owner_id,
            f"▶️ Riprendo la coda ({len(queue.items)} item dal precedente avvio)...",
        )
    _queue_worker_task = asyncio.create_task(
        _queue_worker(client, channel_id, owner_id)
    )


# ─── Upload existing file (no re-download) ───


async def _upload_existing(
    client,
    event,
    status_msg,
    filepath: str,
    title: str,
    channel_id: int,
    url: str = "",
    max_retries: int = 2,
) -> None:
    if not os.path.exists(filepath):
        _clear_progress()
        await _safe_edit(status_msg, "❌ File non più presente. Rimanda il link.")
        return

    file_size_bytes = os.path.getsize(filepath)
    file_size_mb = file_size_bytes / (1024 * 1024)
    if is_over_limit(file_size_bytes):
        _clear_progress()
        await _safe_edit(
            status_msg,
            f"❌ File troppo grande ({file_size_mb / 1024:.1f} GB). "
            f"Limite: {get_max_file_size_bytes() / 1024**3:g}GB.",
        )
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
        pct = (sent / total * 100) if total > 0 else 0
        delta_bytes = sent - tracker.prev_bytes
        delta_t = now - tracker.prev_ts if tracker.prev_ts else 0
        speed_mbps = (
            (delta_bytes / delta_t / 1024 / 1024)
            if delta_t > 0 and delta_bytes > 0
            else 0
        )
        remaining = total - sent
        eta = (
            remaining / (delta_bytes / delta_t)
            if delta_bytes > 0 and delta_t > 0
            else 0
        )
        _set_progress(
            "upload",
            title,
            pct,
            sent / (1024 * 1024),
            total / (1024 * 1024),
            speed_mbps,
            eta,
        )
        if now - last_progress < 5:
            return
        if now < _edit_muted_until:
            return
        last_progress = now
        tracker.prev_bytes = sent
        tracker.prev_ts = now
        text = f"📤 Upload {format_size(sent / (1024 * 1024))} / {format_size(total / (1024 * 1024))} · {pct:.0f}%"
        if speed_mbps > 0:
            text += f"\n▫️ Velocità: {format_speed(speed_mbps)}"
        if eta > 0:
            text += f"\n▫️ Tempo rimanente: {format_eta(eta)}"
        await _safe_edit(status_msg, text)

    await _safe_edit(
        status_msg,
        f"📤 Upload in corso: **{_escape_md(title)}** ({format_size(file_size_mb)})...",
    )

    # Probe real video metadata so Telegram shows it as a playable MP4
    # (with streaming + thumbnail) instead of a generic document.
    loop = asyncio.get_running_loop()
    v_duration, v_w, v_h = await loop.run_in_executor(
        None, probe_video_metadata, filepath
    )

    for attempt in range(1, max_retries + 1):
        try:
            tracker.prev_ts = time_module.time()
            with open(filepath, "rb") as f:
                uploaded = await upload_file(
                    client, f, progress_callback=upload_progress
                )
            send_kwargs = {"file": uploaded, "caption": caption}
            if v_duration or v_w or v_h:
                send_kwargs["attributes"] = [
                    DocumentAttributeVideo(
                        duration=v_duration, w=v_w, h=v_h, supports_streaming=True
                    )
                ]
            await client.send_file(channel_id, **send_kwargs)
            upload_success = True
            _log("Upload esistente completato")
            break
        except FloodWaitError as e:
            _log(f"FloodWait: {e.seconds}s")
            await _safe_edit(
                status_msg,
                f"⏳ Telegram mi ha limitato l'upload per ~{max(1, round(e.seconds / 60))} min"
                f" ({e.seconds}s). Attendo e riprovo in automatico...",
            )
            # Attesa cancellabile: stessa logica del download_and_upload.
            waited = 0
            while waited < e.seconds:
                if _job_cancelled(url):
                    _log("FloodWait _upload_existing interrotto da /stop")
                    break
                step = min(5, e.seconds - waited)
                await asyncio.sleep(step)
                waited += step
        except Exception as e:
            upload_error = repr(e)
            _log(f"Upload tentativo {attempt} fallito: {upload_error}")
            if attempt < max_retries:
                await asyncio.sleep(2 ** (attempt - 1))

    if upload_success:
        _clear_progress()
        await _safe_edit(status_msg, "✅ Video inviato con successo al canale!")
        try:
            _get_history().set_success(url, filepath, title)
        except Exception as e:
            _log(f"history save failed: {e!r}")
    else:
        _clear_progress()
        await _safe_edit(
            status_msg,
            f"❌ Upload fallito dopo {max_retries} tentativi.\nErrore: {upload_error}",
        )
        try:
            if os.path.exists(filepath):
                os.remove(filepath)
        except Exception as e:
            _log(f"Cleanup fallito: {e!r}")
        try:
            _get_history().set_error(url, upload_error or "upload fallito", title)
        except Exception:
            _log("history save fallito (ignorato)")


async def _download_and_upload_saved_media(client, status_msg, msg, channel_id) -> None:
    """Scarica e ricarica nel canale un messaggio con media."""
    media = getattr(msg, "media", None)
    if media is None:
        return
    msg_file = getattr(msg, "file", None)
    raw_name = getattr(msg_file, "name", None) if msg_file else None
    # Il nome puo' non essere una stringa (mock nei test, tipi Telethon
    # inattesi): mai passarlo a re.sub senza controllo.
    fname = raw_name if isinstance(raw_name, str) else None
    mime = getattr(msg_file, "mime_type", None) if msg_file else None
    if not fname:
        ext = {
            "video/mp4": ".mp4",
            "video/quicktime": ".mov",
            "video/x-matroska": ".mkv",
            "audio/mpeg": ".mp3",
            "audio/ogg": ".ogg",
            "application/pdf": ".pdf",
            "image/jpeg": ".jpg",
            "image/png": ".png",
        }.get(mime or "", ".bin")
        fname = f"media_{msg.id}{ext}"
    fname = (
        re.sub(r"[^A-Za-z0-9._ -]", "_", fname).strip(" .")[:150]
        or f"media_{msg.id}.bin"
    )
    filepath = os.path.join("downloads", fname)
    title = (msg.message or "").strip() or os.path.splitext(os.path.basename(filepath))[
        0
    ]
    title = title[:80]
    try:
        os.makedirs("downloads", exist_ok=True)
    except OSError as e:
        _log(f"cartella downloads non creabile: {e!r}")
        await _safe_edit(status_msg, f"\u274c Cartella download non disponibile: {e}")
        return
    loop = asyncio.get_running_loop()
    last_progress = [0.0]

    def progress(received: int, total: int):
        now = time_module.time()
        pct = received / total * 100 if total else 0
        _set_progress(
            "download",
            title,
            pct,
            received / (1024 * 1024),
            total / (1024 * 1024),
            0.0,
            0.0,
        )
        if now - last_progress[0] < 3:
            return
        last_progress[0] = now
        text = f"⬇️ Download: {format_size(received / (1024 * 1024))}"
        if total:
            text += f" / {format_size(total / (1024 * 1024))} · {pct:.0f}%"
        asyncio.run_coroutine_threadsafe(_safe_edit(status_msg, text), loop)

    try:
        if isinstance(media, MessageMediaDocument) and isinstance(
            media.document, Document
        ):
            with open(filepath, "wb") as f:
                await download_file(client, media.document, f, progress)
        else:
            downloaded = await client.download_media(
                msg, file=filepath, progress_callback=progress
            )
            if downloaded:
                filepath = downloaded
        if not os.path.exists(filepath):
            raise ValueError("file non creato")
    except Exception as e:
        _clear_progress()
        _log(f"Media privato: download fallito: {e!r}")
        try:
            if os.path.exists(filepath):
                os.remove(filepath)
        except Exception:
            _log("cleanup media fallito (ignorato)")
        await _safe_edit(status_msg, f"❌ Download fallito: {e}")
        return

    size_gb = os.path.getsize(filepath) / (1024 * 1024 * 1024)
    if size_gb > 2:
        _clear_progress()
        try:
            os.remove(filepath)
        except Exception:
            _log("cleanup media grande fallito (ignorato)")
        await _safe_edit(
            status_msg,
            f"❌ File troppo grande ({size_gb:.1f} GB). Limite Telegram: 2GB",
        )
        return

    if isinstance(media, MessageMediaPhoto):
        try:
            await client.send_file(channel_id, file=filepath, caption=_escape_md(title))
            await _safe_edit(status_msg, "✅ Foto inviata con successo al canale!")
            _clear_progress()
            try:
                _get_history().set_success("", filepath, title)
            except Exception as e:
                _log(f"history save failed: {e!r}")
        except Exception as e:
            _clear_progress()
            _log(f"Upload foto fallito: {e!r}")
            try:
                os.remove(filepath)
            except Exception:
                _log("cleanup foto fallita (ignorata)")
            await _safe_edit(status_msg, f"❌ Upload foto fallito: {e}")
        return

    await _upload_existing(client, None, status_msg, filepath, title, channel_id, "")


async def _process_forwarded_media(client, event, channel_id: int, msg=None) -> None:
    """Scarica e ricarica nel canale un media inviato/inoltrato in privato."""
    msg = msg or event.message
    status_msg = await _safe_reply(event, "📥 Media ricevuto, scarico...")
    if status_msg is None or getattr(msg, "media", None) is None:
        return
    await _download_and_upload_saved_media(client, status_msg, msg, channel_id)


async def download_and_upload_saved(
    client, status_msg, item, channel_id, owner_id
) -> str:
    """Recupera dai Messaggi Salvati e carica il media nel canale."""
    msg = await client.get_messages(item.get("peer", "me"), ids=item.get("msg_id"))
    if msg is None or getattr(msg, "media", None) is None:
        await _safe_edit(
            status_msg, "❌ Messaggio non più disponibile nei Messaggi Salvati."
        )
        _clear_progress()
        return "error"
    await _download_and_upload_saved_media(client, status_msg, msg, channel_id)
    return "ok"


async def cmd_save(client, event, channel_id: int) -> None:
    """Accoda un media dei Messaggi Salvati per il caricamento nel canale."""
    status_msg = await _safe_reply(
        event, "✅ Comando /save ricevuto!\n🔍 Cerco il media..."
    )
    if status_msg is None:
        return

    saved_msg = None
    reply_id = getattr(event.message, "reply_to_msg_id", None)
    if reply_id:
        replied = await client.get_messages("me", ids=reply_id)
        if replied is not None and getattr(replied, "media", None):
            saved_msg = replied
    if saved_msg is None:
        async for message in client.iter_messages("me", limit=50):
            if getattr(message, "media", None):
                saved_msg = message
                break

    if saved_msg is None:
        await _safe_edit(status_msg, "📭 Nessun media trovato nei Messaggi Salvati.")
        return

    msg_file = getattr(saved_msg, "file", None)
    name = (saved_msg.message or "").strip() if saved_msg.message else ""
    name = name or (getattr(msg_file, "name", None) if msg_file else None)
    name = name or "media senza titolo"
    title = name[:80]
    key = f"tg://saved/{saved_msg.id}"
    pos = _get_queue().add(
        key, "original", title, None, kind="saved", msg_id=saved_msg.id
    )
    _log(
        f"/save: accodato media nei Messaggi Salvati (id={saved_msg.id}, nome={title!r})"
    )
    if _current_item is None and pos == 1:
        await _safe_edit(status_msg, f"⏳ Avvio download: **{_escape_md(title)}**...")
    else:
        await _safe_edit(
            status_msg,
            f"📥 Aggiunto alla coda (posizione {pos}): **{_escape_md(title)}**",
        )
    return


# ─── Telegram post link (t.me/...) ───


def _is_telegram_post_link(url: str) -> bool:
    """True se l'URL è un link a un post Telegram (t.me/<canale>/<id>)."""
    return bool(TG_LINK_REGEX.match(url))


async def _process_telegram_link(client, event, url: str, channel_id: int) -> None:
    """Scarica il media di un post Telegram (t.me/...) e lo carica sul canale.

    Il bot è uno userbot: usa la sessione per scaricare direttamente il media
    del post (niente yt-dlp), poi la normale pipeline di upload parallelo.
    Funziona per link pubblici (t.me/username/123) e canali privati dove
    l'account è membro (t.me/c/<id>/<msg>).
    """
    match = TG_LINK_REGEX.match(url)
    if not match:
        await _safe_reply(
            event,
            "❌ Link Telegram non riconosciuto. Formato atteso:\n"
            "`t.me/canale/123` oppure `t.me/c/123456/789`",
        )
        return

    prefix = match.group("prefix")
    entity = match.group("entity")
    try:
        msg_id = int(match.group("msg"))
    except (TypeError, ValueError):
        await _safe_reply(event, "\u274c Link Telegram non valido.")
        return

    status_msg = await _safe_reply(event, "🔍 Recupero del post Telegram...")
    if status_msg is None:
        return

    # ── Risolvi il peer ──
    try:
        if prefix == "c":
            # Canale privato: l'id nel link è positivo, il peer reale è
            # -100<id> (canali) o -<id> (gruppi). Prova tutti i candidati.
            cid = int(entity)
            peer = None
            for candidate in (int(f"-100{cid}"), -cid, cid):
                try:
                    peer = await client.get_entity(PeerChannel(candidate))
                    break
                except Exception as e:
                    _log(f"candidato peer {candidate} fallito: {e!r}")
                    continue
            if peer is None:
                raise ValueError("canale non risolvibile")
        else:
            peer = await client.get_entity(entity)
    except Exception as e:
        _log(f"Telegram link: entity non risolta: {e!r}")
        await _safe_edit(
            status_msg,
            "❌ Canale non accessibile: deve essere pubblico, oppure l'account "
            "del bot deve esserne membro.",
        )
        return

    # ── Recupera il messaggio ──
    try:
        msg = await client.get_messages(peer, ids=msg_id)
    except Exception as e:
        _log(f"Telegram link: messaggio non trovato: {e!r}")
        await _safe_edit(
            status_msg, "❌ Messaggio non trovato: link errato o post cancellato."
        )
        return

    if msg is None or getattr(msg, "media", None) is None:
        await _safe_edit(status_msg, "❌ Il post non contiene media.")
        return

    # ── Nome file ──
    fname = ""
    _tg_file = getattr(msg, "file", None)
    _tg_name = getattr(_tg_file, "name", None) if _tg_file else None
    if isinstance(_tg_name, str) and _tg_name:
        fname = _tg_name
    if not fname:
        ext = ".mp4"
        if isinstance(msg.media, MessageMediaPhoto):
            ext = ".jpg"
        elif getattr(getattr(msg.media, "document", None), "mime_type", ""):
            mime = msg.media.document.mime_type or ""
            mime_ext = {
                "video/mp4": ".mp4",
                "video/x-matroska": ".mkv",
                "image/jpeg": ".jpg",
                "image/png": ".png",
                "application/pdf": ".pdf",
                "application/zip": ".zip",
            }.get(mime, "")
            if mime_ext:
                ext = mime_ext
        fname = f"telegram_{entity}_{msg_id}{ext}"
    safe_name = re.sub(r'[\\/*?:"<>|]', "_", fname)[:150]
    filepath = os.path.join("downloads", safe_name)
    title = (msg.message or "").strip() or safe_name
    title = title[:80]

    # ── Download del media ──
    await _safe_edit(
        status_msg, f"⬇️ Download da Telegram: **{_escape_md(safe_name)}**..."
    )
    loop = asyncio.get_running_loop()
    last_progress = [0.0]

    def progress(received: int, total: int):
        now = time_module.time()
        pct = (received / total * 100) if total else 0
        _set_progress(
            "download",
            title,
            pct,
            received / (1024 * 1024),
            total / (1024 * 1024),
            0.0,
            0.0,
        )
        if now - last_progress[0] < 3:
            return
        last_progress[0] = now
        text = f"⬇️ Download: {format_size(received / (1024 * 1024))}"
        if total:
            text += f" / {format_size(total / (1024 * 1024))} · {pct:.0f}%"
        asyncio.run_coroutine_threadsafe(_safe_edit(status_msg, text), loop)

    try:
        if isinstance(msg.media, MessageMediaDocument) and isinstance(
            msg.media.document, Document
        ):
            with open(filepath, "wb") as f:
                await download_file(client, msg.media.document, f, progress)
        else:
            downloaded = await client.download_media(
                msg, file=filepath, progress_callback=progress
            )
            if downloaded:
                filepath = downloaded
        if not os.path.exists(filepath):
            raise ValueError("file non creato")
    except Exception as e:
        _clear_progress()
        _log(f"Telegram link: download fallito: {e!r}")
        try:
            if os.path.exists(filepath):
                os.remove(filepath)
        except Exception:
            _log("cleanup media fallito (ignorato)")
        await _safe_edit(status_msg, f"❌ Download fallito: {e}")
        return

    size_mb = os.path.getsize(filepath) / (1024 * 1024)
    if is_over_limit(os.path.getsize(filepath)):
        _clear_progress()
        try:
            os.remove(filepath)
        except OSError:
            _log("cleanup media grande fallito (ignorato)")
        await _safe_edit(
            status_msg,
            f"❌ File troppo grande ({size_mb / 1024:.1f} GB). "
            f"Limite Telegram: {get_max_file_size_bytes() / 1024**3:g}GB",
        )
        return

    # Foto: invio diretto (Telethon rileva il tipo).
    if isinstance(msg.media, MessageMediaPhoto):
        caption = _escape_md(title)
        if url:
            caption += f"\n\n🔗 [Post originale]({url.strip()})"
        try:
            await client.send_file(channel_id, file=filepath, caption=caption)
            await _safe_delete(status_msg)
            _clear_progress()
            await _safe_reply(event, "✅ Foto inviata con successo al canale!")
            try:
                _get_history().set_success(url, filepath, title)
            except Exception as e:
                _log(f"history save failed: {e!r}")
        except Exception as e:
            _clear_progress()
            _log(f"Upload foto fallito: {e!r}")
            await _safe_edit(status_msg, f"❌ Upload foto fallito: {e}")
        return

    # Video/documento: pipeline standard (probe + upload parallelo + history).
    await _safe_delete(status_msg)
    status2 = await _safe_reply(event, f"📤 Upload: **{_escape_md(title)}**...")
    await _upload_existing(client, event, status2, filepath, title, channel_id, url)


# ─── Process a new link ───


async def _queue_or_menu(client, event, user_id: int, finfo) -> None:
    """Qualità fissa? Accoda subito (niente menu finto 1-2-3-4 che darebbe
    sempre lo stesso file). Altrimenti mostra il menu qualità."""
    if getattr(finfo, "fixed_quality", False):
        queue = _get_queue()
        title = finfo.title or "Video"
        pos = queue.add(finfo.url, "max", title, finfo.headers or {})
        _log(f"Qualità fissa: accodato {title[:50]}")
        if _current_item is None and pos == 1:
            await _safe_reply(
                event,
                f"⬇️ Avvio download (qualità originale): **{_escape_md(title[:60])}**...",
            )
        elif _current_item is None:
            await _safe_reply(
                event, f"📥 Aggiunto alla coda (posizione {pos}). Avvio a breve."
            )
        else:
            await _safe_reply(
                event,
                f"📥 Aggiunto alla coda (posizione {pos}). "
                f"Verrà scaricato al termine di quello in corso.",
            )
    else:
        await _send_quality_menu(event, user_id, finfo.url, finfo.title, finfo.headers)


async def _process_link(client, event, url: str, channel_id: int) -> None:
    user_id = event.sender_id

    # Link a un post Telegram (t.me/...): scarica il media via sessione userbot.
    if _is_telegram_post_link(url):
        _log(f"Nuovo link Telegram da {user_id}: {url}")
        await _process_telegram_link(client, event, url, channel_id)
        return

    # vidxgo / vidplay player URLs only work embedded inside a parent page
    # (altadefinizione, streamingcommunity). Direct navigation returns 403/404.
    # Tell the user to send the movie/series page URL instead.
    _lower = url.lower()
    if "vidxgo.co" in _lower or "vidplay." in _lower or "vidplaylink" in _lower:
        _log(f"Link player diretto non supportato: {url}")
        await _safe_reply(
            event,
            "❌ Questo è un link del **player** (vidxgo/vidplay) che funziona solo "
            "incorporato nella pagina del sito.\n\n"
            "📎 Invia invece il link della **pagina del film/serie** su "
            "altadefinizione o streamingcommunity, e il bot troverà il video "
            "automaticamente.",
        )
        return

    # Streaming-site extractors (Playwright) take priority over yt-dlp for
    # Cloudflare-protected sites (streamingcommunity, altadefinizione) that
    # yt-dlp can't handle. They return a direct m3u8 URL + the anti-leech
    # headers the CDN requires.
    extractor = get_extractor(url)
    if extractor is not None:
        status_msg = await _safe_reply(event, "🌐 Estrazione video in corso...")
        try:
            async with _extract_lock:
                info = await extractor.extract(url)
            _log(f"Extractor ok: {info.url}")
            await _safe_delete(status_msg)
            await _queue_or_menu(client, event, user_id, info)
        except Exception as e:
            name = getattr(extractor, "__class__", type(extractor)).__name__
            _log(f"Extractor {name} fallito: {e!r}")
            # L'estrattore dedicato può fallire (markup cambiato, token diverso,
            # pagina bloccata): prima di arrendersi provo il fallback universale
            # Playwright, che cattura qualunque stream il sito serva al browser.
            uni_fallback = get_universal_fallback(url)
            if uni_fallback is not None:
                try:
                    finfo = await uni_fallback.extract(url)
                    _log(
                        f"Fallback universale ok dopo extractor dedicato: {finfo.url[:80]}"
                    )
                    await _safe_delete(status_msg)
                    await _queue_or_menu(client, event, user_id, finfo)
                    return
                except Exception as e2:
                    _log(f"Anche il fallback universale è fallito: {e2!r}")
            await _safe_edit(status_msg, f"❌ Estrazione fallita: {e}")
        return

    entry = _get_history().get(url)
    if entry:
        status = entry.get("status")
        title = entry.get("title", "")
        ts = entry.get("ts", 0)
        date_str = (
            time_module.strftime("%d/%m/%Y", time_module.localtime(ts)) if ts else "?"
        )
        if status == "ok":
            filepath = entry.get("filepath", "")
            if os.path.exists(filepath):
                await _safe_reply(
                    event,
                    f"📂 **{_escape_md(title)}** già scaricato (uploadato il {date_str}).\n"
                    f"Scrivi `si` per ricaricarlo, o `no` per annullare.",
                )
                _set_pending(
                    user_id,
                    {
                        "type": "confirm_retry",
                        "action": "reupload",
                        "filepath": filepath,
                        "title": title,
                        "url": url,
                    },
                )
                return
            else:
                await _safe_reply(
                    event,
                    f"📂 **{_escape_md(title)}** è già stato uploadato il {date_str}.\n"
                    f"Scrivi `si` per riscaricarlo, o `no` per annullare.",
                )
                _set_pending(
                    user_id,
                    {
                        "type": "confirm_retry",
                        "action": "retry_download",
                        "url": url,
                        "title": title,
                    },
                )
                return
        elif status == "error":
            err = entry.get("error", "errore sconosciuto")
            await _safe_reply(
                event,
                f"⚠️ Questo link ha dato errore: _{err}_\n"
                f"Scrivi `si` per riprovare o `no` per annullare.",
            )
            _set_pending(
                user_id,
                {
                    "type": "confirm_retry",
                    "action": "retry_download",
                    "url": url,
                    "title": title,
                },
            )
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
            if is_over_limit(info.get("filesize_approx")):
                approx_gb = (info.get("filesize_approx") or 0) / 1024**3
                await _safe_delete(status_msg)
                await _safe_reply(
                    event,
                    f"\u274c Video troppo grande (~{approx_gb:.1f} GB). "
                    f"Limite Telegram: {get_max_file_size_bytes() / 1024**3:g}GB, "
                    "non lo scarico.",
                )
                return
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
            _log(
                "yt-dlp segnala non disponibile: verifico comunque col browser (accesso standard)"
            )
            await _safe_edit(status_msg, "🌐 Verifica disponibilità via browser...")
            try:
                finfo = await fallback.extract(url)
                _log(f"Risorsa servita al browser: {finfo.url[:80]}")
                await _safe_delete(status_msg)
                await _queue_or_menu(client, event, user_id, finfo)
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
            _log(f"yt-dlp fallito ({str(e)[:80]}): provo extractor universale")
            await _safe_edit(status_msg, "🌐 Estrazione via browser (fallback)...")
            try:
                finfo = await fallback.extract(url)
                _log(f"Fallback universale ok: {finfo.url[:80]}")
                await _safe_delete(status_msg)
                await _queue_or_menu(client, event, user_id, finfo)
            except Exception as e2:
                _log(f"Fallback universale fallito: {e2!r}")
                await _safe_edit(status_msg, f"❌ {e}")
            return
        await _safe_edit(status_msg, f"❌ {e}")
    except Exception as e:
        _log(f"Errore analisi: {e!r}")
        await _safe_edit(status_msg, f"❌ Errore: {e}")


# ─── Handle selection ───


async def _handle_selection(
    client, event, user_id: int, text: str, pending: dict, channel_id: int
) -> None:
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
                    _log("history remove fallito (ignorato)")
                status_msg = await _safe_reply(
                    event, f"📤 Invio file esistente: **{_escape_md(title)}**..."
                )
                await _upload_existing(
                    client, event, status_msg, filepath, title, channel_id, url
                )
            elif action == "retry_download":
                url = pending["url"]
                _clear_pending(user_id)
                # Remove history entry so _process_link doesn't loop again
                try:
                    _get_history().remove(url)
                except Exception:
                    _log("history remove fallito (ignorato)")
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
            await _send_playlist_menu(
                event, user_id, videos, pending["url"], page=new_page
            )
            return
        if text_lower in ("prev", "<", "indietro"):
            new_page = max(pending["page"] - 1, 0)
            await _send_playlist_menu(
                event, user_id, videos, pending["url"], page=new_page
            )
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

    if pending["type"] == "batch_quality":
        choice = QUALITY_CHOICES.get(text_lower)
        if text_lower in ("360", "720", "1080", "max"):
            label = "MAX" if text_lower == "max" else f"{text_lower}p"
            quality = text_lower
        elif choice:
            label, quality = choice
        else:
            await _safe_reply(event, "❌ Scrivi `1`, `2`, `3` o `4`.")
            return

        items = pending["items"]
        _clear_pending(user_id)
        queue = _get_queue()
        n = 0
        last_pos = 0
        for it in items:
            last_pos = queue.add(
                it["url"], quality, it["title"], it.get("headers") or {}
            )
            n += 1
        _log(f"Batch: {n} video accodati (qualità {quality}) da {user_id}")
        if _current_item is None and last_pos == 1:
            await _safe_reply(
                event,
                f"⏳ Batch avviato: **{n} download** in qualità **{label}** "
                f"uno per uno in automatico...",
            )
        else:
            await _safe_reply(
                event,
                f"📥 **{n} download in coda** (qualità **{label}**). "
                f"Partono uno per uno in automatico al termine di quelli in corso.",
            )
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
                await _safe_reply(
                    event, f"📥 Aggiunto alla coda (posizione {pos}). Avvio a breve."
                )
        else:
            # Un download è in corso: questo verrà processato dopo.
            await _safe_reply(
                event,
                f"📥 Aggiunto alla coda (posizione {pos}). "
                f"Verrà scaricato al termine di quello in corso.",
            )
        return


async def _process_batch(client, event, urls: list[str], channel_id: int) -> None:
    """Handle multiple links in ONE message: analyze each, show a confirmation
    with the titles, ask ONE quality for all, then queue them one by one.

    The queue worker processes items FIFO, so batch items download
    sequentially in automatic order.
    """
    user_id = event.sender_id
    status_msg = await _safe_reply(event, f"🔍 Analisi batch: {len(urls)} link...")
    items = []  # {url, title, headers}
    skipped = []
    loop = asyncio.get_running_loop()

    for i, url in enumerate(urls, 1):
        try:
            # Extractors dedicati (Playwright) hanno priorità su yt-dlp anche nel batch
            extractor = get_extractor(url)
            if extractor is not None:
                try:
                    async with _extract_lock:
                        finfo = await extractor.extract(url)
                    items.append(
                        {
                            "url": finfo.url,
                            "title": finfo.title or "Video",
                            "headers": finfo.headers or {},
                            "fixed": getattr(finfo, "fixed_quality", False),
                        }
                    )
                    await _safe_edit(
                        status_msg,
                        f"🔍 Analisi batch ({i}/{len(urls)}): {items[-1]['title'][:50]}",
                    )
                    continue
                except Exception:  # pi-lens-ignore: no-boolean-in-except
                    # L'estrattore dedicato ha fallito (markup/token cambiati):
                    # nel batch proviamo comunque il fallback universale browser.
                    fallback = get_universal_fallback(url)
                    if fallback is not None:
                        try:
                            finfo = await fallback.extract(url)
                            items.append(
                                {
                                    "url": finfo.url,
                                    "title": finfo.title or "Video",
                                    "headers": finfo.headers or {},
                                    "fixed": getattr(finfo, "fixed_quality", False),
                                }
                            )
                            await _safe_edit(
                                status_msg,
                                f"🔍 Analisi batch ({i}/{len(urls)}): {items[-1]['title'][:50]}",
                            )
                            continue
                        except Exception as e2:
                            _log(
                                f"Batch: anche il fallback universale è fallito per {url}: {e2!r}"
                            )
                    skipped.append(url)
                    continue
            async with _extract_lock:
                info = await loop.run_in_executor(None, extract_info, url)
            if isinstance(info, list):
                # Playlist: usa il primo titolo come etichetta (scarica la playlist)
                title = (info[0].get("title") if info else None) or "Playlist"
                items.append({"url": url, "title": f"📋 {title}", "headers": {}})
            else:
                title = info.get("title") or "Sconosciuto"
                items.append({"url": url, "title": title, "headers": {}})
        except (ExtractError, VideoUnavailableError):  # pi-lens-ignore: no-boolean-in-except
            # yt-dlp non supporta il sito: prova il fallback universale (browser)
            fallback = get_universal_fallback(url)
            if fallback is not None:
                try:
                    finfo = await fallback.extract(url)
                    items.append(
                        {
                            "url": finfo.url,
                            "title": finfo.title or "Video",
                            "headers": finfo.headers or {},
                            "fixed": getattr(finfo, "fixed_quality", False),
                        }
                    )
                except Exception as e2:
                    _log(f"Batch fallback universale fallito per {url}: {e2!r}")
                    skipped.append(url)
            else:
                skipped.append(url)
        except Exception as e:
            _log(f"Batch analisi fallita per {url}: {e!r}")
            skipped.append(url)
        try:
            current = items[-1]["title"][:50] if items else "..."
            await _safe_edit(
                status_msg, f"🔍 Analisi batch ({i}/{len(urls)}): {current}"
            )
        except Exception:
            _log("aggiornamento batch fallito (ignorato)")

    if not items:
        await _safe_edit(status_msg, "❌ Nessun link valido nel batch.")
        return

    # Se TUTTI i video hanno qualita fissa (es. internetchicks/porn4fans),
    # il menu qualita sarebbe finto: accoda subito in qualita originale.
    if all(it.get("fixed", False) for it in items):
        queue = _get_queue()
        n = 0
        last_pos = 0
        for it in items:
            last_pos = queue.add(
                it["url"], "max", it["title"], it.get("headers") or {}
            )
            n += 1
        _log(f"Batch: {n} video a qualita fissa accodati da {user_id}")
        if _current_item is None and last_pos == 1:
            await _safe_edit(
                status_msg,
                f"⏳ Batch avviato: **{n} download** (qualità originale) uno per uno...",
            )
        else:
            await _safe_edit(
                status_msg,
                f"📥 **{n} download in coda** (qualità originale). "
                f"Partono al termine di quelli in corso.",
            )
        return

    lines = [f"📦 **Batch: {len(items)} video**\n"]
    for idx, it in enumerate(items, 1):
        lines.append(f"{idx}. 🎬 {_escape_md(it['title'][:60])}")
    if skipped:
        lines.append(f"\n⚠️ {len(skipped)} link non elaborabili: {len(skipped)} saltati")
    lines.append("\nScrivi la qualità per TUTTI i video:")
    lines.append("`1`=360p  `2`=720p  `3`=1080p  `4`=MAX")

    await _safe_edit(status_msg, "\n".join(lines))
    _set_pending(user_id, {"type": "batch_quality", "items": items})
    _log(f"Batch pronto: {len(items)} video da {user_id}")


# ─── Message handler ───


async def on_message(
    client, event, whitelist: Whitelist, channel_id: int, owner_id: int
) -> None:
    user_id = event.sender_id
    if user_id is None:
        return
    if user_id != owner_id and not whitelist.is_authorized(user_id):
        return

    text = (event.message.text or "").strip()
    urls = extract_all_urls(text)

    if len(urls) > 1:
        _clear_pending(user_id)
        _log(f"Batch di {len(urls)} link da {user_id}")
        await _process_batch(client, event, urls, channel_id)
        return

    url = urls[0] if urls else None
    if url:
        # Stesso link già in lavorazione o già in coda: niente ri-analisi
        # (ogni risposta alimenta il rate-limit di Telegram e confonde l'utente).
        queue = _get_queue()
        if _current_item is not None and _current_item.get("url") == url:
            _log(f"Link già in lavorazione, ignorato: {url}")
            await _safe_reply(
                event,
                "⏳ Questo link è già **in lavorazione**. Attendi il termine "
                "(scrivi `/status` per lo stato).",
            )
            return
        for it in queue.items:
            if it.get("url") == url:
                _log(f"Link già in coda, ignorato: {url}")
                await _safe_reply(
                    event,
                    "📥 Questo link è **già in coda**. Scrivi `/queue` per vederla.",
                )
                return
        _clear_pending(user_id)
        _log(f"Nuovo link da {user_id}: {url}")
        await _process_link(client, event, url, channel_id)
        return

    if event.message.media:
        await _process_forwarded_media(client, event, channel_id)
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
            display_name = (
                f"@{entity.username}"
                if getattr(entity, "username", None)
                else getattr(entity, "first_name", str(user_id))
            )
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
        lines.append(
            f"🎬 In corso: **{_escape_md((_current_item.get('title') or '')[:55])}** [{_current_item.get('quality', '?')}]"
        )
    else:
        lines.append("🎬 Nessun download in corso.")
    items = queue.items
    lines.append(f"\n📋 In coda ({len(items)}):")
    if not items:
        lines.append("_(vuota)_")
    else:
        for i, it in enumerate(items, 1):
            lines.append(
                f"{i}. {_escape_md((it.get('title') or 'Sconosciuto')[:55])} [{it.get('quality', '?')}]"
            )
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
    await _safe_reply(
        event, f"🧹 Vuoi svuotare la coda ({n} item in attesa)? Scrivi `si` o `no`."
    )
    _set_pending(event.sender_id, {"type": "confirm_clean"})


async def cmd_status(client, event):
    queue = _get_queue()
    pending = len(queue.items)
    ps = _progress_state
    if ps is not None:
        title = (ps.get("title") or (_current_item or {}).get("title") or "Video")[:55]
        pct = max(0.0, min(100.0, float(ps.get("pct", 0))))  # pi-lens-ignore: unchecked-throwing-call-python
        filled = round(pct / 10)
        bar = "■" * filled + "□" * (10 - filled)
        phase = ps.get("phase", "download")
        icon = "⏬" if phase == "download" else "📤"
        label = "Download" if phase == "download" else "Upload"
        line = f"📊 **{_escape_md(title)}**\n{icon} {label} [{bar}] {pct:.0f}%"
        received, total = ps.get("received", 0), ps.get("total", 0)
        if received > 0 or total > 0:
            line += f"\n▫️ {format_size(received)} / {format_size(total)}"
        if ps.get("speed"):
            line += f"\n▫️ Velocità: {format_speed(ps['speed'])}"
        if ps.get("eta"):
            line += f"\n▫️ Tempo rimanente: {format_eta(ps['eta'])}"
    elif _current_item is not None:
        title = (_current_item.get("title") or "Video")[:55]
        line = f"📊 **{_escape_md(title)}**\n_In partenza..._"
    else:
        line = "📊 Nessun download in corso."
    line += f"\n\n📋 In coda ({pending}):"
    if not queue.items:
        line += "\n_(vuota)_"
    else:
        for i, it in enumerate(queue.items, 1):
            line += f"\n{i}. {_escape_md((it.get('title') or 'Sconosciuto')[:55])} [{it.get('quality', '?')}]"
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


def register_handlers(
    client, whitelist: Whitelist, channel_id: int, owner_id: int
) -> None:
    @client.on(events.NewMessage(incoming=True))
    async def _on_message(event):
        if not event.is_private:
            return
        text = event.message.text or ""
        if event.sender_id == owner_id:
            if text.startswith(("/save", "/salva")):
                await cmd_save(client, event, channel_id)
                return
            elif text.startswith("/adduser"):
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
            elif text.startswith("/"):
                await _safe_reply(
                    event,
                    "❌ Comando non riconosciuto.\n"
                    "Comandi disponibili: `/save`, `/adduser`, `/removeuser`, "
                    "`/users`, `/channel`, `/status`, `/stop`, `/queue`, `/now`, `/clean`",
                )
                return
        await on_message(client, event, whitelist, channel_id, owner_id)

    # Messaggi che l'utente scrive nei Messaggi Salvati dell'account del bot
    # (chat con se stesso): sono OUTGOING per l'account, quindi il handler
    # `incoming=True` qui sopra non li vede mai. Li ascoltiamo esplicitamente:
    # riconosciamo solo i comandi /save e /salva (i media inoltrati lì NON
    # vengono elaborati automaticamente, restano come buffer).
    @client.on(events.NewMessage(outgoing=True))
    async def _on_saved_message(event):
        if not event.is_private:
            return
        try:
            me = await client.get_me()
        except Exception:
            return
        if event.chat_id != me.id:
            return
        text = (event.message.text or "").strip()
        if text.startswith(("/save", "/salva")):
            _log(f"Comando nei Messaggi Salvati: {text[:40]!r}")
            await cmd_save(client, event, channel_id)
