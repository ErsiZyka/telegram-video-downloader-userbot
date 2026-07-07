"""Message handlers for the video downloader userbot.

Userbot interaction model (NO callback buttons — those are bot-only):
- User sends a link -> bot shows a numbered menu
- User types the number (plain message, no need to reply) -> bot acts
State is tracked per-user so a plain "2" message selects quality.
"""

import re
import os
import asyncio
import time as time_module
from pyrogram import Client, filters
from pyrogram.types import Message
from pyrogram.errors import FloodWait

from bot.whitelist import Whitelist
from bot.downloader import (
    extract_info,
    download_video,
    DownloadError,
    ExtractError,
    format_size,
    format_speed,
    format_eta,
    cleanup_orphan_files,
)
from bot.history import DownloadHistory

URL_REGEX = r"(https?://[^\s]+)"

# ─── Download concurrency control ───
_is_downloading = False
_cancel_requested = False  # set by /stop command


class CancelDownload(Exception):
    """Raised to interrupt an in-progress download/upload."""

# ─── Per-user pending action state ───
# user_id -> {"type": "quality"|"playlist", ..., "ts": timestamp}
_pending: dict[int, dict] = {}
PENDING_TIMEOUT = 600  # 10 minutes

# ─── Download history (persistent URL → outcome cache, lazy init) ───
_history: DownloadHistory | None = None


def _get_history() -> DownloadHistory:
    global _history
    if _history is None:
        _history = DownloadHistory(filepath="data/download_history.json")
    return _history

# ─── Quality selection map (number -> (label, quality_key)) ───
QUALITY_CHOICES = {
    "1": ("360p", "360"),
    "2": ("720p", "720"),
    "3": ("1080p", "1080"),
    "4": ("MAX", "max"),
}


def _log(msg: str) -> None:
    """Visible log line so the user can see what the bot is doing."""
    try:
        print(f"[bot] {msg}", flush=True)
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
    """Extract the first URL from a text message. Returns None if no URL found."""
    match = re.search(URL_REGEX, text)
    return match.group(1) if match else None


def _escape_md(text: str) -> str:
    """Escape markdown special chars so dynamic content (titles) renders literally."""
    if not text:
        return ""
    for ch in ("\\", "*", "_", "`", "[", "]"):
        text = text.replace(ch, f"\\{ch}")
    return text


# ─── Safe message helpers (log instead of silently swallowing) ───

async def _safe_edit(msg: Message, text: str) -> None:
    try:
        await msg.edit_text(text)
    except Exception as e:
        _log(f"edit_text fallito (ignorato): {e!r}")


async def _safe_delete(msg: Message) -> None:
    try:
        await msg.delete()
    except Exception as e:
        _log(f"delete fallito (ignorato): {e!r}")


# ─── Menu senders ───

async def _send_quality_menu(message: Message, user_id: int, url: str, title: str) -> None:
    menu = await message.reply_text(
        f"🎬 **{_escape_md(title)}**\n\n"
        f"Scrivi qui il NUMERO della qualità:\n"
        f"1️⃣ 360p\n"
        f"2️⃣ 720p\n"
        f"3️⃣ 1080p\n"
        f"🔥 4️⃣ MAX"
    )
    _set_pending(user_id, {"type": "quality", "url": url, "title": title})
    _log(f"Menu qualità inviato a {user_id} (msg {menu.id})")


async def _send_playlist_menu(
    message: Message, user_id: int, videos: list[dict], url: str, page: int = 0,
) -> None:
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

    menu = await message.reply_text("\n".join(lines))
    _set_pending(user_id, {
        "type": "playlist", "videos": videos, "url": url, "page": page,
    })
    _log(f"Menu playlist inviato a {user_id} (msg {menu.id}, {total} video)")


# ─── Core: download_and_upload ───

async def download_and_upload(
    client: Client,
    status_msg: Message,
    url: str,
    quality: str,
    title: str,
    channel_id: int,
    max_retries: int = 3,
) -> None:
    """Full download → upload → cleanup flow with progress, retry, and error handling."""
    global _is_downloading

    if _is_downloading:
        await status_msg.reply_text("⏳ C'è già un download in corso. Riprova tra poco.")
        return
    _is_downloading = True
    global _cancel_requested
    _cancel_requested = False  # reset for this download
    _log(f"Download iniziato: {url} [{quality}]")

    loop = asyncio.get_running_loop()

    try:
        last_progress_update = 0.0
        progress_interval = 2

        def progress_callback(downloaded_mb: float, total_mb: float, speed_mbps: float, eta) -> None:
            nonlocal last_progress_update
            if _cancel_requested:
                raise CancelDownload("stop")
            now = time_module.time()
            if now - last_progress_update < progress_interval:
                return
            last_progress_update = now
            pct = (downloaded_mb / total_mb * 100) if total_mb > 0 else 0
            text = f"⏳ **Download in corso...**\n▫️ {format_size(downloaded_mb)}"
            if total_mb > 0:
                text += f" / {format_size(total_mb)}"
            text += f"\n▫️ Velocità: {format_speed(speed_mbps)}"
            if eta and int(eta) > 0:
                text += f"\n▫️ Tempo rimanente: {format_eta(eta)}"
            text += f"\n▫️ {pct:.0f}%"
            asyncio.run_coroutine_threadsafe(_safe_edit(status_msg, text), loop)

        # ─── Download phase ───
        filepath = None
        download_error = None

        for attempt in range(1, max_retries + 1):
            if _cancel_requested:
                cleanup_orphan_files()
                await _safe_edit(status_msg, "🛑 Download annullato.")
                return
            try:
                if attempt > 1:
                    await _safe_edit(status_msg, f"⏳ Download (tentativo {attempt}/{max_retries})...")
                filepath = await loop.run_in_executor(
                    None, download_video, url, quality, progress_callback, 1,
                )
                _log(f"Download completato: {filepath}")
                break
            except CancelDownload:
                cleanup_orphan_files()
                await _safe_edit(status_msg, "🛑 Download annullato.")
                return
            except DownloadError as e:
                download_error = str(e)
                _log(f"Tentativo {attempt} fallito: {download_error}")
                if attempt < max_retries:
                    await asyncio.sleep(2 ** (attempt - 1))
            except Exception as e:
                if _cancel_requested:
                    cleanup_orphan_files()
                    await _safe_edit(status_msg, "🛑 Download annullato.")
                    return
                download_error = repr(e)
                _log(f"Errore imprevisto download (tentativo {attempt}): {download_error}")
                cleanup_orphan_files()
                await _safe_edit(status_msg, f"❌ Download fallito: {e}")
                try:
                    _get_history().set_error(url, str(e), title)
                except Exception:
                    pass
                return

        if filepath is None:
            cleanup_orphan_files()
            await _safe_edit(
                status_msg,
                f"❌ Download fallito dopo {max_retries} tentativi.\nErrore: {download_error}",
            )
            try:
                _get_history().set_error(url, download_error or "download fallito", title)
            except Exception:
                pass
            return

        # ─── Size check (Telegram 2GB limit) ───
        file_size_bytes = os.path.getsize(filepath) if os.path.exists(filepath) else 0
        file_size_mb = file_size_bytes / (1024 * 1024)
        file_size_gb = file_size_mb / 1024

        if file_size_gb > 2:
            if os.path.exists(filepath):
                os.remove(filepath)
            await _safe_edit(
                status_msg,
                f"❌ Il file supera il limite di Telegram (2GB).\nDimensione: {file_size_gb:.1f} GB",
            )
            return

        # ─── Upload phase ───
        from bot.downloader import format_size as fmt_sz
        caption = _escape_md(title) if title else "Video scaricato"
        upload_success = False
        upload_error = None
        last_upload_progress = 0.0

        class _UpTracker:
            prev_bytes: int = 0
            prev_ts: float = 0.0
            last_update: float = 0.0

        tracker = _UpTracker()

        def upload_progress(current: int, total: int) -> None:
            nonlocal last_upload_progress
            if _cancel_requested:
                raise CancelDownload("stop")
            now = time_module.time()
            if now - last_upload_progress < 2:
                return
            last_upload_progress = now
            pct = (current / total * 100) if total > 0 else 0
            delta_bytes = current - tracker.prev_bytes
            delta_t = now - tracker.prev_ts if tracker.prev_ts else 0
            speed_mbps = (delta_bytes / delta_t / 1024 / 1024) if delta_t > 0 and delta_bytes > 0 else 0
            remaining = total - current
            eta = remaining / (delta_bytes / delta_t) if delta_bytes > 0 and delta_t > 0 else 0
            tracker.prev_bytes = current
            tracker.prev_ts = now
            text = (
                f"✅ Download completato ({format_size(file_size_mb)})\n"
                f"📤 Upload {fmt_sz(current/(1024*1024))} / {fmt_sz(total/(1024*1024))} · {pct:.0f}%"
            )
            if speed_mbps > 0:
                text += f"\n▫️ Velocità: {format_speed(speed_mbps)}"
            if eta > 0:
                text += f"\n▫️ Tempo rimanente: {format_eta(eta)}"
            asyncio.run_coroutine_threadsafe(_safe_edit(status_msg, text), loop)

        for attempt in range(1, max_retries + 1):
            try:
                if attempt > 1:
                    await _safe_edit(status_msg, f"📤 Upload (tentativo {attempt}/{max_retries})...")
                else:
                    await _safe_edit(
                        status_msg,
                        f"✅ Download completato ({format_size(file_size_mb)})\n"
                        f"📤 Upload in corso al canale...",
                    )
                tracker.prev_ts = time_module.time()
                await client.send_video(
                    chat_id=channel_id, video=filepath, caption=caption, supports_streaming=True,
                    progress=upload_progress,
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
                return
            except FloodWait as e:
                _log(f"FloodWait upload: attendo {e.value}s")
                await asyncio.sleep(e.value)
            except Exception as e:
                if _cancel_requested:
                    if os.path.exists(filepath):
                        try:
                            os.remove(filepath)
                        except Exception:
                            pass
                    await _safe_edit(status_msg, "🛑 Upload annullato. File eliminato.")
                    return
                upload_error = repr(e)
                _log(f"Upload tentativo {attempt} fallito: {upload_error}")
                if attempt < max_retries:
                    await asyncio.sleep(2 ** (attempt - 1))

        # ─── Cleanup ───
        if os.path.exists(filepath):
            try:
                os.remove(filepath)
            except Exception as e:
                _log(f"Cleanup file fallito: {e!r}")

        if upload_success:
            await _safe_edit(status_msg, "✅ Video inviato con successo al canale!")
        else:
            await _safe_edit(
                status_msg,
                f"❌ Upload fallito dopo {max_retries} tentativi.\nErrore: {upload_error}",
            )

    finally:
        _is_downloading = False


# ─── Upload an existing file (no re-download) ───

async def _upload_existing(
    client: Client,
    status_msg: Message,
    filepath: str,
    title: str,
    channel_id: int,
    url: str = "",
    max_retries: int = 2,
) -> None:
    """Upload a file that was already downloaded, with progress bar."""
    import time as time_module
    loop = asyncio.get_running_loop()

    if not os.path.exists(filepath):
        await _safe_edit(status_msg, "❌ File non più presente su disco. Rimanda il link per riscaricarlo.")
        return

    file_size_bytes = os.path.getsize(filepath)
    file_size_mb = file_size_bytes / (1024 * 1024)
    file_size_gb = file_size_mb / 1024

    if file_size_gb > 2:
        await _safe_edit(status_msg, f"❌ File troppo grande ({file_size_gb:.1f} GB).")
        return

    caption = _escape_md(title) if title else "Video"
    upload_success = False
    upload_error = None
    last_progress = 0.0

    class _UpTracker:
        prev_bytes: int = 0
        prev_ts: float = 0.0
        last_update: float = 0.0

    tracker = _UpTracker()

    def upload_progress(current: int, total: int) -> None:
        nonlocal last_progress
        now = time_module.time()
        if now - last_progress < 2:
            return
        last_progress = now
        pct = (current / total * 100) if total > 0 else 0
        delta_bytes = current - tracker.prev_bytes
        delta_t = now - tracker.prev_ts if tracker.prev_ts else 0
        speed_mbps = (delta_bytes / delta_t / 1024 / 1024) if delta_t > 0 and delta_bytes > 0 else 0
        remaining = total - current
        eta = remaining / (delta_bytes / delta_t) if delta_bytes > 0 and delta_t > 0 else 0
        tracker.prev_bytes = current
        tracker.prev_ts = now
        text = (
            f"📤 Upload {format_size(current/(1024*1024))} / {format_size(total/(1024*1024))} · {pct:.0f}%"
        )
        if speed_mbps > 0:
            text += f"\n▫️ Velocità: {format_speed(speed_mbps)}"
        if eta > 0:
            text += f"\n▫️ Tempo rimanente: {format_eta(eta)}"
        asyncio.run_coroutine_threadsafe(_safe_edit(status_msg, text), loop)

    await _safe_edit(status_msg, f"📤 Upload in corso: **{_escape_md(title)}** ({format_size(file_size_mb)})...")

    for attempt in range(1, max_retries + 1):
        try:
            if attempt > 1:
                await _safe_edit(status_msg, f"📤 Upload (tentativo {attempt}/{max_retries})...")
            tracker.prev_ts = time_module.time()
            await client.send_video(
                chat_id=channel_id, video=filepath, caption=caption,
                supports_streaming=True, progress=upload_progress,
            )
            upload_success = True
            _log("Upload esistente completato")
            break
        except FloodWait as e:
            _log(f"FloodWait upload: {e.value}s")
            await asyncio.sleep(e.value)
        except Exception as e:
            upload_error = repr(e)
            _log(f"Upload tentativo {attempt} fallito: {upload_error}")
            if attempt < max_retries:
                await asyncio.sleep(2 ** (attempt - 1))

    if upload_success:
        await _safe_edit(status_msg, "✅ Video inviato con successo al canale!")
        # Record success in history
        try:
            _get_history().set_success(url, filepath, title)
        except Exception as e:
            _log(f"history save failed: {e!r}")
    else:
        await _safe_edit(
            status_msg,
            f"❌ Upload fallito dopo {max_retries} tentativi.\nErrore: {upload_error}",
        )
        try:
            _get_history().set_error(url, upload_error or "upload fallito", title)
        except Exception:
            pass


# ─── Process a new link ───

async def _process_link(client: Client, message: Message, url: str, channel_id: int) -> None:
    user_id = message.from_user.id

    # ─── Check download history ───
    entry = _get_history().get(url)
    if entry:
        status = entry.get("status")
        title = entry.get("title", "")
        ts = entry.get("ts", 0)
        date_str = time_module.strftime("%d/%m/%Y", time_module.localtime(ts)) if ts else "?"
        if status == "ok":
            filepath = entry.get("filepath", "")
            if os.path.exists(filepath):
                # File still on disk → offer re-upload without re-downloading
                await message.reply_text(
                    f"📂 **{_escape_md(title)}** giá scaricato (uploadato il {date_str}).\n"
                    f"Scrivi `si` per ricaricarlo subito (senza riscaricare), o `no` per annullare."
                )
                _set_pending(user_id, {
                    "type": "confirm_retry",
                    "action": "reupload",
                    "filepath": filepath,
                    "title": title,
                    "url": url,
                })
                return
            else:
                # File cleaned up → warn and offer re-download
                await message.reply_text(
                    f"📂 **{_escape_md(title)}** é giá stato uploadato il {date_str} (file rimosso dal disco).\n"
                    f"Scrivi `si` per riscaricarlo e ricaricarlo, o `no` per annullare."
                )
                _set_pending(user_id, {
                    "type": "confirm_retry",
                    "action": "retry_download",
                    "url": url,
                    "title": title,
                })
                return
        elif status == "error":
            err = entry.get("error", "errore sconosciuto")
            await message.reply_text(
                f"⚠️ Questo link ha dato errore in precedenza: _{err}_\n"
                f"Scrivi `si` per riprovare o `no` per annullare."
            )
            _set_pending(user_id, {
                "type": "confirm_retry",
                "action": "retry_download",
                "url": url,
                "title": title,
            })
            return

    # ─── Normal flow: extract info ───
    status_msg = await message.reply_text("🔍 Analisi del link in corso...")
    try:
        loop = asyncio.get_running_loop()
        info = await loop.run_in_executor(None, extract_info, url)
        if isinstance(info, list):
            if not info:
                await _safe_edit(status_msg, "❌ Playlist vuota o senza video accessibili.")
                return
            await _safe_delete(status_msg)
            await _send_playlist_menu(message, user_id, info, url, page=0)
        else:
            title = info.get("title", "Sconosciuto")
            await _safe_delete(status_msg)
            await _send_quality_menu(message, user_id, url, title)
    except ExtractError as e:
        _log(f"ExtractError: {e}")
        await _safe_edit(status_msg, f"❌ {e}")
    except Exception as e:
        _log(f"Errore analisi link: {e!r}")
        await _safe_edit(status_msg, f"❌ Errore durante l'analisi: {e}")


# ─── Handle a selection message (plain number, not reply) ───

async def _handle_selection(
    client: Client, message: Message, user_id: int, text: str, pending: dict, channel_id: int,
) -> None:
    text_lower = text.lower().strip()

    # ─── Confirm retry (si/no for duplicate/error links) ───
    if pending["type"] == "confirm_retry":
        if text_lower in ("si", "sì", "yes", "y"):
            action = pending["action"]
            if action == "reupload":
                # Upload existing file without re-downloading
                filepath = pending["filepath"]
                title = pending["title"]
                url = pending.get("url", "")
                _clear_pending(user_id)
                _log(f"Re-upload richiesto: {filepath}")
                status_msg = await message.reply_text(f"📤 Invio file esistente: **{_escape_md(title)}**...")
                await _upload_existing(client, status_msg, filepath, title, channel_id, url)
            elif action == "retry_download":
                # Retry the full download process
                url = pending["url"]
                _clear_pending(user_id)
                _log(f"Retry download: {url}")
                status_msg = await message.reply_text("🔍 Analisi del link in corso...")
                await _process_link(client, message, url, channel_id)
            else:
                _clear_pending(user_id)
        else:
            _clear_pending(user_id)
            await message.reply_text("👌 Operazione annullata.")
        return

    # ─── Playlist menu ───
    if pending["type"] == "playlist":
        videos = pending["videos"]
        page_size = 10
        total_pages = (len(videos) + page_size - 1) // page_size or 1

        if text_lower in ("next", ">", "avanti"):
            new_page = min(pending["page"] + 1, total_pages - 1)
            await _send_playlist_menu(message, user_id, videos, pending["url"], page=new_page)
            return
        if text_lower in ("prev", "<", "indietro"):
            new_page = max(pending["page"] - 1, 0)
            await _send_playlist_menu(message, user_id, videos, pending["url"], page=new_page)
            return

        try:
            num = int(text_lower)
        except ValueError:
            await message.reply_text("❌ Scrivi il NUMERO del video (o `next`/`prev`).")
            return
        if not (1 <= num <= len(videos)):
            await message.reply_text(f"❌ Numero tra 1 e {len(videos)}.")
            return

        video = videos[num - 1]
        video_url = video.get("webpage_url") or video.get("url", "")
        if not video_url:
            await message.reply_text("❌ URL del video non disponibile.")
            return

        status_msg = await message.reply_text("🔍 Analisi del video...")
        try:
            loop = asyncio.get_running_loop()
            info = await loop.run_in_executor(None, extract_info, video_url)
            if isinstance(info, list):
                info = info[0] if info else {}
            title = info.get("title", "Sconosciuto")
            await _safe_delete(status_msg)
            await _send_quality_menu(message, user_id, video_url, title)
        except ExtractError as e:
            await _safe_edit(status_msg, f"❌ {e}")
        except Exception as e:
            _log(f"Errore analisi video playlist: {e!r}")
            await _safe_edit(status_msg, f"❌ Errore: {e}")
        return

    # ─── Quality menu ───
    if pending["type"] == "quality":
        choice = QUALITY_CHOICES.get(text_lower)
        if text_lower in ("360", "720", "1080", "max"):
            label = "MAX" if text_lower == "max" else f"{text_lower}p"
            quality = text_lower
        elif choice:
            label, quality = choice
        else:
            await message.reply_text("❌ Scrivi `1`, `2`, `3` o `4`.")
            return

        url = pending["url"]
        title = pending["title"]
        _clear_pending(user_id)
        _log(f"Qualità scelta da {user_id}: {quality}")
        status_msg = await message.reply_text(f"⏳ Avvio download in qualità **{label}**...")
        await download_and_upload(client, status_msg, url, quality, title, channel_id)
        return


# ─── Message handler ───

async def on_message(
    client: Client,
    message: Message,
    whitelist: Whitelist,
    channel_id: int,
    owner_id: int,
) -> None:
    """Dispatch: new links start the flow; plain numbers select from pending menu."""
    user = message.from_user
    if not user:
        return
    if user.id != owner_id and not whitelist.is_authorized(user.id):
        return

    text = (message.text or "").strip()
    url = extract_url(text)

    if url:
        # New link — clear any pending state and process
        _clear_pending(user.id)
        _log(f"Nuovo link da {user.id}: {url}")
        await _process_link(client, message, url, channel_id)
        return

    # Not a URL — is there a pending menu for this user?
    pending = _get_pending(user.id)
    if pending:
        await _handle_selection(client, message, user.id, text, pending, channel_id)
        return

    _log(f"Messaggio senza menu pending ignorato da {user.id}: {text[:40]!r}")


# ─── Admin commands ───

async def cmd_adduser(client: Client, message: Message, whitelist: Whitelist, owner_id: int) -> None:
    if not message.from_user or message.from_user.id != owner_id:
        return
    parts = (message.text or "").split()
    if len(parts) < 2:
        await message.reply_text("❌ Uso: `/adduser @username` o `/adduser 123456789`")
        return
    target = parts[1]
    if target.startswith("@"):
        try:
            user = await client.get_users(target)
            user_id = user.id
            display_name = f"@{user.username}" if user.username else user.first_name
        except Exception as e:
            _log(f"get_users fallito per {target}: {e!r}")
            await message.reply_text(f"❌ Impossibile trovare `{target}`.")
            return
    else:
        try:
            user_id = int(target)
            display_name = str(user_id)
        except ValueError:
            await message.reply_text("❌ Formato non valido.")
            return
    if whitelist.is_authorized(user_id):
        await message.reply_text(f"ℹ️ {display_name} è già autorizzato.")
        return
    whitelist.add(user_id, username=target, added_by=str(message.from_user.id))
    await message.reply_text(f"✅ {display_name} aggiunto alla whitelist.")


async def cmd_removeuser(client: Client, message: Message, whitelist: Whitelist, owner_id: int) -> None:
    if not message.from_user or message.from_user.id != owner_id:
        return
    parts = (message.text or "").split()
    if len(parts) < 2:
        await message.reply_text("❌ Uso: `/removeuser @username` o `/removeuser 123456789`")
        return
    target = parts[1]
    if target.startswith("@"):
        user_id = None
        for uid, udata in whitelist.get_all().items():
            if udata.get("username", "").lower() == target.lower():
                user_id = uid
                break
        if user_id is None:
            await message.reply_text(f"❌ {target} non trovato.")
            return
    else:
        try:
            user_id = int(target)
        except ValueError:
            await message.reply_text("❌ Formato non valido.")
            return
    if whitelist.remove(user_id):
        await message.reply_text(f"✅ {target} rimosso.")
    else:
        await message.reply_text(f"❌ {target} non era nella whitelist.")


async def cmd_users(client: Client, message: Message, whitelist: Whitelist, owner_id: int) -> None:
    if not message.from_user or message.from_user.id != owner_id:
        return
    users = whitelist.get_all()
    if not users:
        await message.reply_text("📭 Nessun utente nella whitelist.")
        return
    lines = []
    for uid, udata in users.items():
        username = udata.get("username", str(uid))
        added_at = udata.get("added_at", "?")[:10]
        lines.append(f"• `{uid}` — {username} (dal {added_at})")
    await message.reply_text(f"**📋 Autorizzati ({len(users)}):**\n" + "\n".join(lines))


async def cmd_channel(client: Client, message: Message, channel_id: int) -> None:
    try:
        chat = await client.get_chat(channel_id)
        name = chat.title or str(channel_id)
        await message.reply_text(f"📺 Canale: **{name}** (`{channel_id}`)")
    except Exception:
        await message.reply_text(f"📺 Canale: `{channel_id}`")


async def cmd_status(client: Client, message: Message):
    await message.reply_text(f"📊 Download in corso: {'sì' if _is_downloading else 'no'}")


async def cmd_stop(client: Client, message: Message, owner_id: int) -> None:
    """Stop any in-progress download/upload and clean up files."""
    if not message.from_user or message.from_user.id != owner_id:
        return
    global _cancel_requested
    if _is_downloading:
        _cancel_requested = True
        await message.reply_text("🛑 Interruzione richiesta... il processo verrà fermato al prossimo ciclo.")
        _log("Stop richiesto dall'utente")
    else:
        # Nothing in progress: just clean any leftover files
        removed = cleanup_orphan_files()
        await message.reply_text(
            f"📭 Nessun download in corso. Puliti {len(removed)} file residui."
        )


# ─── Handler registration ───

def register_handlers(app: Client, whitelist: Whitelist, channel_id: int, owner_id: int) -> None:
    @app.on_message(filters.text & filters.private)
    async def _on_message(client, message):
        if message.from_user and message.from_user.id == owner_id:
            text = message.text or ""
            if text.startswith("/adduser"):
                await cmd_adduser(client, message, whitelist, owner_id)
                return
            elif text.startswith("/removeuser"):
                await cmd_removeuser(client, message, whitelist, owner_id)
                return
            elif text.startswith("/users"):
                await cmd_users(client, message, whitelist, owner_id)
                return
            elif text.startswith("/channel"):
                await cmd_channel(client, message, channel_id)
                return
            elif text.startswith("/status"):
                await cmd_status(client, message)
                return
            elif text.startswith("/stop"):
                await cmd_stop(client, message, owner_id)
                return
        await on_message(client, message, whitelist, channel_id, owner_id)
