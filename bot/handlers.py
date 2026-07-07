"""Message handlers for the video downloader userbot (reply-based, no buttons).

NOTE: Userbots (user accounts) cannot receive callback queries — inline buttons
do NOT work. All interaction is done via replies to menu messages.
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

URL_REGEX = r"(https?://[^\s]+)"

# ─── Download concurrency control ───
_is_downloading = False

# ─── Pending menus: maps menu message_id -> state ───
# state = {"type": "quality", "url": ..., "title": ...}
#       | {"type": "playlist", "videos": [...], "url": ..., "page": int}
_pending_menus: dict[int, dict] = {}

# ─── Quality selection map (number -> (label, quality_key)) ───
QUALITY_CHOICES = {
    "1": ("360p", "360"),
    "2": ("720p", "720"),
    "3": ("1080p", "1080"),
    "4": ("MAX", "max"),
}


def extract_url(text: str) -> str | None:
    """Extract the first URL from a text message. Returns None if no URL found."""
    match = re.search(URL_REGEX, text)
    return match.group(1) if match else None


# ─── Helpers for safe message editing (user accounts CAN edit their own messages) ───

async def _safe_edit(msg: Message, text: str) -> None:
    try:
        await msg.edit_text(text)
    except Exception:
        pass


async def _safe_delete(msg: Message) -> None:
    try:
        await msg.delete()
    except Exception:
        pass


def _escape_md(text: str) -> str:
    """Escape markdown special chars so dynamic content (titles) renders literally.

    Video titles from yt-dlp often contain *, _, ` etc. which break Pyrogram's
    default markdown parsing. Without escaping, sending the message raises
    BadRequest and the message never reaches the user.
    """
    if not text:
        return ""
    for ch in ("\\", "*", "_", "`", "[", "]"):
        text = text.replace(ch, f"\\{ch}")
    return text


# ─── Menu builders (send text menus, store state keyed by message id) ───

async def send_quality_menu(message: Message, url: str, title: str) -> None:
    """Send a quality-selection menu as a reply; user replies with the number."""
    menu = await message.reply_text(
        f"🎬 **{_escape_md(title)}**\n\n"
        f"Scegli la qualità (RISPONDI a questo messaggio col numero):\n"
        f"1️⃣ 360p\n"
        f"2️⃣ 720p\n"
        f"3️⃣ 1080p\n"
        f"🔥 4️⃣ MAX\n\n"
        f"Esempio: rispondi con `2` per 720p."
    )
    _pending_menus[menu.id] = {"type": "quality", "url": url, "title": title}


async def send_playlist_menu(
    message: Message, videos: list[dict], url: str, page: int = 0, page_size: int = 10,
) -> None:
    """Send a numbered playlist menu; user replies with the video number."""
    total = len(videos)
    total_pages = (total + page_size - 1) // page_size or 1
    start = page * page_size
    page_videos = videos[start:start + page_size]

    lines = [f"📋 **Playlist trovata**: {total} video\n"]
    lines.append(f"_RISPONDI col numero del video da scaricare_ (pagina {page + 1}/{total_pages}):\n")
    for i, v in enumerate(page_videos):
        idx = start + i + 1
        vtitle = _escape_md((v.get("title") or "Sconosciuto")[:60])
        dur = v.get("duration", 0)
        dur_str = f" • {dur // 60}:{dur % 60:02d}" if dur else ""
        lines.append(f"{idx}. {vtitle}{dur_str}")

    lines.append("")
    if total_pages > 1:
        lines.append("Per cambiare pagina rispondi con `next` o `prev`.")

    menu = await message.reply_text("\n".join(lines))
    _pending_menus[menu.id] = {
        "type": "playlist", "videos": videos, "url": url, "page": page,
    }


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
        caption = _escape_md(title) if title else "Video scaricato"
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
                await client.send_video(chat_id=channel_id, video=filepath, caption=caption, supports_streaming=True)
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


# ─── Message handler (NEW links + reply selections) ───

async def on_message(
    client: Client,
    message: Message,
    whitelist: Whitelist,
    channel_id: int,
) -> None:
    """Handle incoming private text messages: replies to menus OR new links."""
    if not message.from_user or not whitelist.is_authorized(message.from_user.id):
        return

    text = (message.text or "").strip()

    # ─── Check if this is a reply to a pending menu ───
    reply_to_id = message.reply_to_message_id
    has_url = extract_url(text) is not None
    if reply_to_id and reply_to_id in _pending_menus and not has_url:
        await _handle_selection(client, message, text, channel_id)
        return

    # ─── Otherwise treat as a new link ───
    url = extract_url(text)
    if not url:
        return

    status_msg = await message.reply_text("🔍 Analisi del link in corso...")

    try:
        loop = asyncio.get_running_loop()
        info = await loop.run_in_executor(None, extract_info, url)

        if isinstance(info, list):
            if not info:
                await _safe_edit(status_msg, "❌ La playlist è vuota o non contiene video accessibili.")
                return
            await _safe_delete(status_msg)
            await send_playlist_menu(message, info, url, page=0)
        else:
            title = info.get("title", "Sconosciuto")
            await _safe_delete(status_msg)
            await send_quality_menu(message, url, title)
    except ExtractError as e:
        await _safe_edit(status_msg, f"❌ {str(e)}")
    except Exception as e:
        await _safe_edit(status_msg, f"❌ Errore durante l'analisi: {str(e)}")


async def _handle_selection(client: Client, message: Message, text: str, channel_id: int) -> None:
    """Handle a reply to a pending menu (quality or playlist selection)."""
    menu_id = message.reply_to_message_id
    state = _pending_menus.get(menu_id)
    if not state:
        return

    text_lower = text.lower().strip()

    # ─── Playlist menu reply ───
    if state["type"] == "playlist":
        videos = state["videos"]
        page = state["page"]

        # Pagination
        if text_lower in ("next", ">", "avanti", "su"):
            page_size = 10
            total_pages = (len(videos) + page_size - 1) // page_size or 1
            new_page = min(page + 1, total_pages - 1)
            _pending_menus.pop(menu_id, None)
            await send_playlist_menu(message, videos, state["url"], page=new_page)
            return
        if text_lower in ("prev", "<", "indietro", "giù", "giu"):
            new_page = max(state["page"] - 1, 0)
            _pending_menus.pop(menu_id, None)
            await send_playlist_menu(message, videos, state["url"], page=new_page)
            return

        # Video number
        try:
            num = int(text)
        except ValueError:
            await message.reply_text("❌ Numero non valido. Rispondi col numero del video.")
            return
        if num < 1 or num > len(videos):
            await message.reply_text(f"❌ Numero fuori range. Vanno da 1 a {len(videos)}.")
            return

        video = videos[num - 1]
        video_url = video.get("webpage_url") or video.get("url", "")
        if not video_url:
            await message.reply_text("❌ URL del video non disponibile.")
            return

        # Resolve the single video, then show quality menu
        status_msg = await message.reply_text("🔍 Analisi del video...")
        try:
            loop = asyncio.get_running_loop()
            info = await loop.run_in_executor(None, extract_info, video_url)
            if isinstance(info, list):
                info = info[0] if info else {}
            title = info.get("title", "Sconosciuto")
            _pending_menus.pop(menu_id, None)
            await _safe_delete(status_msg)
            await send_quality_menu(message, video_url, title)
        except ExtractError as e:
            await _safe_edit(status_msg, f"❌ {str(e)}")
        except Exception as e:
            await _safe_edit(status_msg, f"❌ Errore: {str(e)}")
        return

    # ─── Quality menu reply ───
    if state["type"] == "quality":
        # Accept "1".."4" or direct "360"/"720"/"1080"/"max"
        choice = QUALITY_CHOICES.get(text_lower)
        if text_lower in ("360", "720", "1080", "max"):
            label = "MAX" if text_lower == "max" else f"{text_lower}p"
            quality = text_lower
        elif choice:
            label, quality = choice
        else:
            await message.reply_text(
                "❌ Scelta non valida. Rispondi con `1`, `2`, `3` o `4` "
                "(oppure `360`, `720`, `1080`, `max`)."
            )
            return

        url = state["url"]
        title = state["title"]
        _pending_menus.pop(menu_id, None)

        status_msg = await message.reply_text(f"⏳ Avvio download in qualità **{label}**...")
        await download_and_upload(client, status_msg, url, quality, title, channel_id)
        return


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
    """Register the message handler on the Pyrogram client."""

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
