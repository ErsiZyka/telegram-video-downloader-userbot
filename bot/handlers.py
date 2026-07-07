"""Message and callback handlers for the video downloader userbot."""

import re
import os
import asyncio
from pyrogram import Client, filters
from pyrogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.errors import FloodWait

from bot.whitelist import Whitelist
from bot.downloader import (
    extract_info,
    download_video,
    DownloadError,
    ExtractError,
    QUALITY_FORMATS,
    cleanup_orphan_files,
)

URL_REGEX = r"(https?://[^\s]+)"

# Global download queue to prevent concurrent downloads
_download_queue: list[asyncio.Task] = []


def extract_url(text: str) -> str | None:
    """Extract the first URL from a text message. Returns None if no URL found."""
    match = re.search(URL_REGEX, text)
    return match.group(1) if match else None


# ─── Callback data format ───
# "action:param1|param2"
# - quality:720|url
# - playlist:playlist_url|video_index
# - page:page_num
# - cancel

def parse_callback_data(data: str) -> tuple[str, str | None, str | None]:
    """
    Parse callback data into (action, url_or_param, extra).
    """
    if data == "cancel":
        return ("cancel", None, None)

    if ":" not in data:
        return (data, None, None)

    action, rest = data.split(":", 1)
    if "|" in rest:
        param1, param2 = rest.split("|", 1)
    else:
        param1, param2 = rest, None

    return (action, param1, param2)


def build_quality_keyboard(url: str) -> InlineKeyboardMarkup:
    """Build an inline keyboard with quality options for a given URL."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("360p", callback_data=f"quality:360|{url}"),
            InlineKeyboardButton("720p", callback_data=f"quality:720|{url}"),
        ],
        [
            InlineKeyboardButton("1080p", callback_data=f"quality:1080|{url}"),
            InlineKeyboardButton("🔥 MAX", callback_data=f"quality:max|{url}"),
        ],
        [
            InlineKeyboardButton("❌ Annulla", callback_data="cancel"),
        ],
    ])


def build_playlist_keyboard(
    videos: list[dict],
    page: int = 0,
    page_size: int = 10,
) -> InlineKeyboardMarkup:
    """
    Build an inline keyboard for a playlist with pagination.
    """
    total_pages = (len(videos) + page_size - 1) // page_size
    start = page * page_size
    end = start + page_size
    page_videos = videos[start:end]

    rows = []
    for i, video in enumerate(page_videos):
        idx = start + i
        title = (video.get("title") or "Sconosciuto")[:50]
        duration = video.get("duration", 0)
        if duration:
            mins, secs = divmod(duration, 60)
            title = f"{title} • {mins}:{secs:02d}"

        rows.append([
            InlineKeyboardButton(
                title[:45],
                callback_data=f"playlist:{video.get('webpage_url', '')}|{idx}",
            )
        ])

    # Pagination row
    nav_buttons = []
    if page > 0:
        nav_buttons.append(
            InlineKeyboardButton("◀️ Precedenti", callback_data=f"page:{page - 1}")
        )
    if page < total_pages - 1:
        nav_buttons.append(
            InlineKeyboardButton("Avanti ▶️", callback_data=f"page:{page + 1}")
        )
    if nav_buttons:
        rows.append(nav_buttons)

    rows.append([InlineKeyboardButton("❌ Annulla", callback_data="cancel")])

    return InlineKeyboardMarkup(rows)


# ─── Core: download_and_upload (stub until Task 8) ───

async def download_and_upload(
    client: Client,
    status_msg: Message,
    url: str,
    quality: str,
    title: str,
    channel_id: int,
    max_retries: int = 3,
) -> None:
    """
    Full download → upload → cleanup flow with progress, retry, and error handling.
    """
    import time as time_module

    # Register this download in the queue
    current_task = asyncio.current_task()
    if current_task:
        _download_queue.append(current_task)

    try:
        last_progress_update = 0
        progress_interval = 2

        def progress_callback(downloaded_mb: float, total_mb: float, speed_mbps: float, eta: int) -> None:
            nonlocal last_progress_update
            now = time_module.time()
            if now - last_progress_update < progress_interval:
                return
            last_progress_update = now

            from bot.downloader import format_size, format_speed, format_eta

            pct = (downloaded_mb / total_mb * 100) if total_mb > 0 else 0
            text = (
                f"⏳ **Download in corso...**\n"
                f"▫️ {format_size(downloaded_mb)}"
            )
            if total_mb > 0:
                text += f" / {format_size(total_mb)}"
            text += f"\n▫️ Velocità: {format_speed(speed_mbps)}"
            if eta and int(eta) > 0:
                text += f"\n▫️ Tempo rimanente: {format_eta(eta)}"
            text += f"\n▫️ {pct:.0f}%"

            asyncio.get_event_loop().create_task(
                status_msg.edit_text(text)
            )

        # ─── Download phase ───
        filepath = None
        download_error = None

        for attempt in range(1, max_retries + 1):
            try:
                if attempt > 1:
                    await status_msg.edit_text(
                        f"⏳ Download (tentativo {attempt}/{max_retries})..."
                    )

                loop = asyncio.get_event_loop()
                filepath = await loop.run_in_executor(
                    None,
                    download_video,
                    url,
                    quality,
                    progress_callback,
                    1,
                )
                break
            except DownloadError as e:
                download_error = str(e)
                if attempt < max_retries:
                    wait = 2 ** (attempt - 1)
                    await asyncio.sleep(wait)
            except Exception as e:
                download_error = str(e)
                cleanup_orphan_files()
                await status_msg.edit_text(f"❌ Download fallito: {download_error}")
                return

        if filepath is None:
            cleanup_orphan_files()
            await status_msg.edit_text(
                f"❌ Download fallito dopo {max_retries} tentativi.\n"
                f"Errore: {download_error}"
            )
            return

        # Check file size before upload
        file_size_bytes = os.path.getsize(filepath) if os.path.exists(filepath) else 0
        file_size_mb = file_size_bytes / (1024 * 1024)
        file_size_gb = file_size_mb / 1024

        if file_size_gb > 2:
            os.remove(filepath)
            await status_msg.edit_text(
                f"❌ Il file scaricato supera il limite di Telegram (2GB).\n"
                f"Dimensione: {file_size_gb:.1f} GB"
            )
            return

        # ─── Upload phase ───
        from bot.downloader import format_size

        caption = title if title else "Video scaricato"
        upload_success = False
        upload_error = None

        for attempt in range(1, max_retries + 1):
            try:
                if attempt > 1:
                    await status_msg.edit_text(
                        f"📤 Upload (tentativo {attempt}/{max_retries})..."
                    )
                else:
                    await status_msg.edit_text(
                        f"✅ Download completato ({format_size(file_size_mb)})\n"
                        f"📤 Upload in corso al canale..."
                    )

                await client.send_video(
                    chat_id=channel_id,
                    video=filepath,
                    caption=caption,
                )
                upload_success = True
                break
            except FloodWait as e:
                await asyncio.sleep(e.value)
            except Exception as e:
                upload_error = str(e)
                if attempt < max_retries:
                    wait = 2 ** (attempt - 1)
                    await asyncio.sleep(wait)

        # ─── Cleanup ───
        if os.path.exists(filepath):
            os.remove(filepath)

        if upload_success:
            await status_msg.edit_text("✅ Video inviato con successo al canale!")
        else:
            await status_msg.edit_text(
                f"❌ Upload fallito dopo {max_retries} tentativi.\n"
                f"Errore: {upload_error}\n"
                f"File eliminato dal disco."
            )

    finally:
        # Remove from queue when done
        if current_task and current_task in _download_queue:
            _download_queue.remove(current_task)


# ─── Message handler ───

async def on_message(
    client: Client,
    message: Message,
    whitelist: Whitelist,
    channel_id: int,
) -> None:
    """Handle incoming private text messages from whitelisted users."""
    if not whitelist.is_authorized(message.from_user.id):
        return

    url = extract_url(message.text or "")
    if not url:
        return

    # ─── Queue gate: prevent concurrent downloads ───
    global _download_queue
    _download_queue = [t for t in _download_queue if not t.done()]

    if _download_queue:
        position = len(_download_queue) + 1
        await message.reply_text(
            f"⏳ C'è già un download in corso. "
            f"Il tuo link è in coda (posizione #{position}). "
            f"Riprova quando il download corrente è completato."
        )
        return

    status_msg = await message.reply_text("🔍 Analisi del link in corso...")

    try:
        loop = asyncio.get_event_loop()
        info = await loop.run_in_executor(None, extract_info, url)

        if isinstance(info, list):
            # Playlist
            if len(info) == 0:
                await status_msg.edit_text("❌ La playlist è vuota o non contiene video accessibili.")
                return

            keyboard = build_playlist_keyboard(info, page=0)
            total = len(info)
            _playlist_cache[str(status_msg.id)] = {"videos": info, "url": url}
            await status_msg.edit_text(
                f"📋 **Playlist trovata**: {total} video\n"
                f"Scegli quali video scaricare:",
                reply_markup=keyboard,
            )
        else:
            # Single video
            title = info.get("title", "Sconosciuto")
            duration = info.get("duration", 0)
            uploader = info.get("uploader", "")
            filesize = info.get("filesize_approx", 0)

            mins, secs = divmod(duration, 60)
            dur_str = f"{mins}:{secs:02d}" if duration else "N/D"

            text = f"🎬 **{title}**\n"
            if uploader:
                text += f"📺 {uploader}\n"
            text += f"⏱ {dur_str}"
            if filesize:
                text += f" • ~{filesize / (1024*1024):.0f} MB"
            text += "\n\nScegli la qualità:"

            keyboard = build_quality_keyboard(url)
            await status_msg.edit_text(text, reply_markup=keyboard)

    except ExtractError as e:
        await status_msg.edit_text(f"❌ {str(e)}")
    except Exception as e:
        await status_msg.edit_text(f"❌ Errore durante l'analisi: {str(e)}")


# ─── Callback handler ───

_playlist_cache: dict[str, dict] = {}


async def on_callback(
    client: Client,
    callback: CallbackQuery,
    whitelist: Whitelist,
    channel_id: int,
) -> None:
    """Handle all inline button callbacks."""
    if not whitelist.is_authorized(callback.from_user.id):
        await callback.answer("Non sei autorizzato.", show_alert=True)
        return

    data = callback.data or ""
    action, param1, param2 = parse_callback_data(data)

    if action == "cancel":
        await callback.message.delete()
        await callback.answer()
        return

    if action == "page":
        page = int(param1)
        cache_key = str(callback.message.id)
        if cache_key in _playlist_cache:
            cached = _playlist_cache[cache_key]
            videos = cached["videos"]
            keyboard = build_playlist_keyboard(videos, page=page)
            total = len(videos)
            await callback.message.edit_text(
                f"📋 **Playlist trovata**: {total} video\n"
                f"Scegli quali video scaricare:",
                reply_markup=keyboard,
            )
        else:
            await callback.answer("Playlist non più disponibile. Rimanda il link.", show_alert=True)
        await callback.answer()
        return

    if action == "quality":
        quality = param2
        url = param1
        await callback.answer(f"Download in {quality}p...")
        await callback.message.edit_text(
            f"⏳ Avvio download in qualità **{quality}**...",
            reply_markup=None,
        )
        await download_and_upload(
            client, callback.message, url, quality, "", channel_id,
        )
        return

    if action == "playlist":
        video_url = param1

        await callback.answer("Analisi del video...")
        status_msg = await callback.message.reply_text("🔍 Analisi del video...")

        try:
            loop = asyncio.get_event_loop()
            info = await loop.run_in_executor(None, extract_info, video_url)

            if isinstance(info, list):
                info = info[0] if info else {}

            title = info.get("title", "Sconosciuto")
            keyboard = build_quality_keyboard(video_url)

            await status_msg.edit_text(
                f"🎬 **{title}**\nScegli la qualità:",
                reply_markup=keyboard,
            )
        except ExtractError as e:
            await status_msg.edit_text(f"❌ {str(e)}")
        except Exception as e:
            await status_msg.edit_text(f"❌ Errore: {str(e)}")

        return

    await callback.answer("Azione sconosciuta.")


# ─── Admin commands (stubs — Task 9) ───

async def cmd_adduser(client: Client, message: Message, whitelist: Whitelist, owner_id: int):
    await message.reply_text("Comando /adduser — da implementare nel Task 9.")


async def cmd_removeuser(client: Client, message: Message, whitelist: Whitelist, owner_id: int):
    await message.reply_text("Comando /removeuser — da implementare nel Task 9.")


async def cmd_users(client: Client, message: Message, whitelist: Whitelist, owner_id: int):
    await message.reply_text("Comando /users — da implementare nel Task 9.")


async def cmd_channel(client: Client, message: Message, channel_id: int):
    await message.reply_text(f"Canale corrente: `{channel_id}`")


async def cmd_status(client: Client, message: Message):
    queue_len = len(_download_queue)
    active = sum(1 for t in _download_queue if not t.done())
    await message.reply_text(f"📊 Download attivi: {active}\n📋 In coda: {queue_len - active}")


# ─── Handler registration ───

def register_handlers(app: Client, whitelist: Whitelist, channel_id: int, owner_id: int) -> None:
    """Register all message and callback handlers on the Pyrogram client."""

    @app.on_message(filters.text & filters.private)
    async def _on_message(client, message):
        # Admin commands (only from owner)
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

        await on_message(client, message, whitelist, channel_id)

    @app.on_callback_query()
    async def _on_callback(client, callback):
        if not whitelist.is_authorized(callback.from_user.id):
            await callback.answer("Non sei autorizzato.", show_alert=True)
            return
        await on_callback(client, callback, whitelist, channel_id)
