#!/usr/bin/env python3
"""
Telegram Video Downloader Userbot — Entry Point.

Usage:
    1. Copy .env.example to .env and fill in your credentials.
    2. Run: python run.py
    3. First run: enter phone number and verification code.
    4. Subsequent runs: session file auto-loads.
"""

import os
import sys
import asyncio

# Force UTF-8 on Windows console to avoid UnicodeEncodeError on titles/filenames
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from dotenv import load_dotenv

from pyrogram import idle

from bot.client import create_client
from bot.whitelist import Whitelist
from bot.handlers import register_handlers
from bot.downloader import check_dependencies, cleanup_orphan_files


def _load_config() -> dict:
    """Load and validate configuration from .env. Returns config dict or exits."""
    api_id_str = os.getenv("API_ID", "")
    api_hash = os.getenv("API_HASH", "")
    channel_id_str = os.getenv("CHANNEL_ID") or os.getenv("CHANNEL_USERNAME", "")
    owner_id_str = os.getenv("OWNER_ID", "")

    errors = []
    if not api_id_str:
        errors.append("API_ID non impostato nel file .env")
    if not api_hash:
        errors.append("API_HASH non impostato nel file .env")
    if not channel_id_str:
        errors.append("CHANNEL_ID (o CHANNEL_USERNAME) non impostato nel file .env")
    if not owner_id_str:
        errors.append("OWNER_ID non impostato nel file .env")

    if errors:
        print("ERRORI DI CONFIGURAZIONE:")
        for e in errors:
            print(f"  - {e}")
        print("\nCopia .env.example in .env e compila tutti i campi.")
        sys.exit(1)

    try:
        api_id = int(api_id_str)
        owner_id = int(owner_id_str)
        if channel_id_str.startswith("@"):
            channel_id = channel_id_str
        else:
            channel_id = int(channel_id_str)
    except ValueError as e:
        print(f"Errore: un valore numerico nel .env non e' valido: {e}")
        sys.exit(1)

    return {
        "api_id": api_id,
        "api_hash": api_hash,
        "channel_id": channel_id,
        "owner_id": owner_id,
        "download_dir": os.getenv("DOWNLOAD_DIR", "downloads/"),
        "max_retries": int(os.getenv("MAX_RETRIES", "3")),
    }


async def main() -> None:
    """Main async entry point."""
    load_dotenv()
    config = _load_config()

    download_dir = config["download_dir"]
    os.makedirs(download_dir, exist_ok=True)

    # ─── Check dependencies ───
    print("Verifica dipendenze...")
    deps_ok, deps_msg = check_dependencies()
    if not deps_ok:
        print(f"ERRORE: {deps_msg}")
        sys.exit(1)
    print("Dipendenze OK")

    # ─── Cleanup orphan files from previous runs ───
    removed = cleanup_orphan_files(download_dir)
    if removed:
        print(f"Puliti {len(removed)} file orfani da {download_dir}")

    # ─── Initialize whitelist ───
    whitelist = Whitelist(filepath="data/whitelist.json")

    # ─── Create Pyrogram client ───
    app = create_client(config["api_id"], config["api_hash"])

    # ─── Register handlers ───
    register_handlers(app, whitelist, config["channel_id"], config["owner_id"])

    # ─── Start ───
    await app.start()
    print("Userbot avviato e in ascolto...")
    print(f"   Canale destinazione: {config['channel_id']}")
    print(f"   Owner ID: {config['owner_id']}")
    print(f"   Download dir: {download_dir}")

    # ─── Pre-resolve the target channel (warm up the session peer cache) ───
    # A fresh session has an empty peer cache. Fetching dialogs forces Pyrogram
    # to cache all chats/channels the account is a member of, including the
    # target channel. This prevents "Peer id invalid" errors.
    try:
        print("Preparazione cache peer...")
        async for _ in app.get_dialogs():
            pass
        print("Cache peer popolata.")
    except Exception as e:
        print(f"ATTENZIONE: impossibile leggere i dialoghi ({e}).")

    try:
        chat = await app.get_chat(config["channel_id"])
        name = chat.title if chat else config["channel_id"]
        print(f"Canale destinazione risolto: {name}")
    except Exception as e:
        print(f"ATTENZIONE: impossibile pre-risolvere il canale ({e}).")
        print("Assicurati che l'userbot sia admin del canale e che il CHANNEL_ID sia corretto.")
        print("Se il canale e' pubblico, usa CHANNEL_USERNAME=@nomecanale nel .env.")

    # ─── Run until stopped ───
    await idle()
    await app.stop()
    print("Userbot fermato.")


if __name__ == "__main__":
    asyncio.run(main())
