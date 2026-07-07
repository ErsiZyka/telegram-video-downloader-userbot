"""Message and callback handlers for the video downloader userbot."""

import re
import os
import asyncio
import time as time_module
from pyrogram import Client, filters
from pyrogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
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

URL_REGEX = r"(https?://[^\s]+)"

# ─── Download concurrency control ───
_is_downloading = False

# ─── URL cache: maps short key -> full URL (Telegram callback_data <= 64 bytes) ───
_url_cache: dict[str, str] = {}
_url_cache_counter = 0


def cache_url(url: str) -> str:
    """Store a URL and return a short key safe for callback_data (<=64 bytes)."""
    for key, val in _url_cache.items():
        if val == url:
            return key
    global _url_cache_counter
    _url_cache_counter += 1
    key = f"u{_url_cache_counter}"
    _url_cache[key] = url
    return key


def get_cached_url(key: str) -> str | None:
    """Resolve a cached URL key back to the full URL."""
    return _url_cache.get(key)


def extract_url(text: str) -> str | None:
    """Extract the first URL from a text message. Returns None if no URL found."""
    match = re.search(URL_REGEX, text)
    return match.group(1) if match else None


# ─── Callback data format ───
# "action:param1|param2"
# - quality:QUALITY|URL_KEY   (e.g. "quality:720|u3")
# - playlist:URL_KEY|index    (e.g. "playlist:u5|0")
# - page:PAGE_NUM             (e.g. "page:1")
# - cancel

def parse_callback_data(data: str) -> tuple[str, str | None, str | None]:
    """Parse callback data into (action, param1, param2)."""
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
    key = cache_url(url)
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("360p", callback_data=f"quality:360|{key}"),
            InlineKeyboardButton("720p", callback_data=f"quality:720|{key}"),
        ],
        [
            InlineKeyboardButton("1080p", callback_data=f"quality:1080|{key}"),
            InlineKeyboardButton("🔥 MAX", callback_data=f"quality:max|{key}"),
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
    """Build an inline keyboard for a playlist with pagination."""
    total_pages = (len(videos) + page_size - 1) // page_size or 1
    start = page * page_size
    page_videos = videos[start:start + page_size]

    rows = []
    for i, video in enumerate(page_videos):
        title = (video.get("title") or "Sconosciuto")[:50]
        duration = video.get("duration", 0)
        if duration:
            mins, secs = divmod(duration, 60)
            title = f"{title} • {mins}:{secs:02d}"
        video_url = video.get("webpage_url") or video.get("url", "")
        key = cache_url(video_url)
        idx = start + i
        rows.append([
            InlineKeyboardButton(title[:45], callback_data=f"playlist:{key}|{idx}")
        ])

    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton("◀️ Precedenti", callback_data=f"page:{page - 1}"))
    if page < total_pages - 1:
        nav_buttons.append(InlineKeyboardButton("Avanti ▶️", callback_data=f"page:{page + 1}"))
    if nav_buttons:
        rows.append(nav_buttons)
    rows.append([InlineKeyboardButton("❌ Annulla", callback_data="cancel")])
    return InlineKeyboardMarkup(rows)


# ─── Helpers for safe message editing (no "exception never retrieved") ───

async def _safe_edit(msg: Message, text: str) -> None:
    """Edit a message, ignoring transient errors (message deleted, not modified)."""
    try:
        await msg.edit_text(text)
    except Exception:
        pass


async def _safe_delete(msg: Message) -> None:
    """Delete a message, ignoring errors if it no longer exists."""
    try:
        await msg.delete()
    except Exception:
        pass


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

    # ─── Concurrency gate: one download at a time ───
    if _is_downloading:
        await _safe_edit(status_msg, "⏳ C'è già un download in corso. Riprova tra poco.")
        return
    _is_downloading = True

    # Capture the running event loop so the (thread-based) progress hook can talk to it
    loop = asyncio.get_running_loop()

    try:
        last_progress_update = 0.0
        progress_interval = 2

        def progress_callback(downloaded_mb: float, total_mb: float, speed_mbps: float, eta) -> None:
            nonlocal last_progress_update
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

            # Thread-safe: schedule the edit on the main event loop
            asyncio.run_coroutine_threadsafe(_safe_edit(status_msg, text), loop)

        # ─── Download phase ───
        filepath = None
        download_error = None

        for attempt in range(1, max_retries + 1):
            try:
                if attempt > 1:
                    await _safe_edit(status_msg, f"⏳ Download (tentativo {attempt}/{max_retries})...")
                filepath = await loop.run_in_executor(
                    None, download_video, url, quality, progress_callback, 1,
                )
                break
            except DownloadError as e:
                download_error = str(e)
                if attempt < max_retries:
                    await asyncio.sleep(2 ** (attempt - 1))
            except Exception as e:
                download_error = str(e)
                cleanup_orphan_files()
                await _safe_edit(status_msg, f"❌ Download fallito: {download_error}")
                return

        if filepath is None:
            cleanup_orphan_files()
            await _safe_edit(
                status_msg,
                f"❌ Download fallito dopo {max_retries} tentativi.\nErrore: {download_error}",
            )
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
        caption = title if title else "Video scaricato"
        upload_success = False
        upload_error = None

        for attempt in range(1, max_retries + 1):
            try:
                if attempt > 1:
                    await _safe_edit(status_msg, f"📤 Upload (tentativo {attempt}/{max_retries})...")
                else:
                    await _safe_edit(
                        status_msg,
                        f"✅ Download completato ({format_size(file_size_mb)})\n📤 Upload in corso al canale...",
                    )
                await client.send_video(chat_id=channel_id, video=filepath, caption=caption)
                upload_success = True
                break
            except FloodWait as e:
                await asyncio.sleep(e.value)
            except Exception as e:
                upload_error = str(e)
                if attempt < max_retries:
                    await asyncio.sleep(2 ** (attempt - 1))

        # ─── Cleanup ───
        if os.path.exists(filepath):
            os.remove(filepath)

        if upload_success:
            await _safe_edit(status_msg, "✅ Video inviato con successo al canale!")
        else:
            await _safe_edit(
                status_msg,
                f"❌ Upload fallito dopo {max_retries} tentativi.\nErrore: {upload_error}\nFile eliminato dal disco.",
            )

    finally:
        _is_downloading = False


# ─── Message handler ───

async def on_message(
    client: Client,
    message: Message,
    whitelist: Whitelist,
    channel_id: int,
) -> None:
    """Handle incoming private text messages from whitelisted users."""
    if not message.from_user or not whitelist.is_authorized(message.from_user.id):
        return

    url = extract_url(message.text or "")
    if not url:
        return

    status_msg = await message.reply_text("🔍 Analisi del link in corso...")

    try:
        loop = asyncio.get_running_loop()
        info = await loop.run_in_executor(None, extract_info, url)

        if isinstance(info, list):
            # Playlist
            if not info:
                await _safe_edit(status_msg, "❌ La playlist è vuota o non contiene video accessibili.")
                return
            keyboard = build_playlist_keyboard(info, page=0)
            total = len(info)
            await _safe_delete(status_msg)
            new_msg = await message.reply_text(
                f"📋 **Playlist trovata**: {total} video\nScegli quali video scaricare:",
                reply_markup=keyboard,
            )
            _playlist_cache[str(new_msg.id)] = {"videos": info, "url": url}
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
                text += f" • ~{filesize / (1024 * 1024):.0f} MB"
            text += "\n\nScegli la qualità:"

            keyboard = build_quality_keyboard(url)
            # Cache the title keyed the same way the keyboard does, for the caption
            url_key = cache_url(url)
            _title_cache[url_key] = title
            await _safe_delete(status_msg)
            await message.reply_text(text, reply_markup=keyboard)

    except ExtractError as e:
        await _safe_edit(status_msg, f"❌ {str(e)}")
    except Exception as e:
        await _safe_edit(status_msg, f"❌ Errore durante l'analisi: {str(e)}")


# ─── Callback handler ───

_playlist_cache: dict[str, dict] = {}
_title_cache: dict[str, str] = {}


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

    try:
        if action == "cancel":
            if callback.message:
                await _safe_delete(callback.message)
            await callback.answer()
            return

        if action == "page":
            page = int(param1)
            cache_key = str(callback.message.id) if callback.message else ""
            cached = _playlist_cache.get(cache_key)
            if cached and callback.message:
                keyboard = build_playlist_keyboard(cached["videos"], page=page)
                total = len(cached["videos"])
                await callback.message.edit_text(
                    f"📋 **Playlist trovata**: {total} video\nScegli quali video scaricare:",
                    reply_markup=keyboard,
                )
                await callback.answer()
            else:
                await callback.answer("Playlist non più disponibile. Rimanda il link.", show_alert=True)
            return

        if action == "quality":
            quality = param2 or "720"
            url_key = param1
            url = get_cached_url(url_key) or ""
            title = _title_cache.get(url_key, "")
            if not url:
                await callback.answer("Link scaduto. Rimanda il messaggio.", show_alert=True)
                return
            await callback.answer(f"Download {quality}...")
            if callback.message:
                await callback.message.edit_text(
                    f"⏳ Avvio download in qualità **{quality}**...", reply_markup=None,
                )
                await download_and_upload(
                    client, callback.message, url, quality, title, channel_id,
                )
            return

        if action == "playlist":
            url_key = param1
            video_url = get_cached_url(url_key) or ""
            if not video_url:
                await callback.answer("Link scaduto. Rimanda il messaggio.", show_alert=True)
                return
            await callback.answer("Analisi del video...")
            if not callback.message:
                return
            status_msg = await callback.message.reply_text("🔍 Analisi del video...")
            try:
                loop = asyncio.get_running_loop()
                info = await loop.run_in_executor(None, extract_info, video_url)
                if isinstance(info, list):
                    info = info[0] if info else {}
                title = info.get("title", "Sconosciuto")
                keyboard = build_quality_keyboard(video_url)
                _title_cache[cache_url(video_url)] = title
                await _safe_delete(status_msg)
                await callback.message.reply_text(
                    f"🎬 **{title}**\nScegli la qualità:", reply_markup=keyboard,
                )
            except ExtractError as e:
                await _safe_edit(status_msg, f"❌ {str(e)}")
            except Exception as e:
                await _safe_edit(status_msg, f"❌ Errore: {str(e)}")
            return

        await callback.answer("Azione sconosciuta.")
    except Exception as e:
        # Never let a callback error bubble up unhandled
        try:
            await callback.answer(f"❌ Errore: {str(e)[:100]}", show_alert=True)
        except Exception:
            pass


# ─── Admin commands ───

async def cmd_adduser(client: Client, message: Message, whitelist: Whitelist, owner_id: int) -> None:
    """Add a user to the whitelist. Usage: /adduser @username or /adduser 123456789"""
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
        except Exception:
            await message.reply_text(
                f"❌ Impossibile trovare l'utente `{target}`. "
                f"Assicurati che abbia mai interagito con questo account."
            )
            return
    else:
        try:
            user_id = int(target)
            display_name = str(user_id)
        except ValueError:
            await message.reply_text("❌ Formato non valido. Usa `/adduser @username` o `/adduser 123456789`")
            return

    if whitelist.is_authorized(user_id):
        await message.reply_text(f"ℹ️ {display_name} è già autorizzato.")
        return
    whitelist.add(user_id, username=target, added_by=str(message.from_user.id))
    await message.reply_text(f"✅ {display_name} aggiunto alla whitelist.")


async def cmd_removeuser(client: Client, message: Message, whitelist: Whitelist, owner_id: int) -> None:
    """Remove a user from the whitelist."""
    if not message.from_user or message.from_user.id != owner_id:
        return
    parts = (message.text or "").split()
    if len(parts) < 2:
        await message.reply_text("❌ Uso: `/removeuser @username` o `/removeuser 123456789`")
        return
    target = parts[1]

    if target.startswith("@"):
        all_users = whitelist.get_all()
        user_id = None
        for uid, udata in all_users.items():
            if udata.get("username", "").lower() == target.lower():
                user_id = uid
                break
        if user_id is None:
            await message.reply_text(f"❌ {target} non trovato nella whitelist.")
            return
    else:
        try:
            user_id = int(target)
        except ValueError:
            await message.reply_text("❌ Formato non valido.")
            return

    if whitelist.remove(user_id):
        await message.reply_text(f"✅ {target} rimosso dalla whitelist.")
    else:
        await message.reply_text(f"❌ {target} non era nella whitelist.")


async def cmd_users(client: Client, message: Message, whitelist: Whitelist, owner_id: int) -> None:
    """List all whitelisted users."""
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
    text = f"**📋 Utenti autorizzati ({len(users)}):**\n" + "\n".join(lines)
    await message.reply_text(text)


async def cmd_channel(client: Client, message: Message, channel_id: int) -> None:
    """Show the current target channel."""
    try:
        chat = await client.get_chat(channel_id)
        name = chat.title or str(channel_id)
        await message.reply_text(f"📺 Canale di destinazione: **{name}** (`{channel_id}`)")
    except Exception:
        await message.reply_text(f"📺 Canale di destinazione: `{channel_id}`")


async def cmd_status(client: Client, message: Message):
    await message.reply_text(
        f"📊 Download in corso: {'sì' if _is_downloading else 'no'}"
    )


# ─── Handler registration ───

def register_handlers(app: Client, whitelist: Whitelist, channel_id: int, owner_id: int) -> None:
    """Register all message and callback handlers on the Pyrogram client."""

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
        await on_message(client, message, whitelist, channel_id)

    @app.on_callback_query()
    async def _on_callback(client, callback):
        if not whitelist.is_authorized(callback.from_user.id):
            await callback.answer("Non sei autorizzato.", show_alert=True)
            return
        await on_callback(client, callback, whitelist, channel_id)
