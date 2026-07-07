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
from dotenv import load_dotenv

from bot.client import create_client
from bot.whitelist import Whitelist
from bot.handlers import register_handlers
from bot.downloader import check_dependencies, cleanup_orphan_files


def main() -> None:
    """Main entry point. Load config, validate, start the userbot."""
    # Load .env
    load_dotenv()

    # ─── Validate required env vars ───
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
        print("❌ ERRORI DI CONFIGURAZIONE:")
        for e in errors:
            print(f"  • {e}")
        print("\nCopia .env.example in .env e compila tutti i campi.")
        sys.exit(1)

    try:
        api_id = int(api_id_str)
        owner_id = int(owner_id_str)

        # Parse channel_id: if it's a username like @channel, use as-is
        if channel_id_str.startswith("@"):
            channel_id = channel_id_str
        else:
            channel_id = int(channel_id_str)
    except ValueError as e:
        print(f"❌ Errore: un valore numerico nel .env non è valido: {e}")
        sys.exit(1)

    # Optional config
    download_dir = os.getenv("DOWNLOAD_DIR", "downloads/")
    max_retries = int(os.getenv("MAX_RETRIES", "3"))

    # Ensure download directory exists
    os.makedirs(download_dir, exist_ok=True)

    # ─── Check dependencies ───
    print("🔍 Verifica dipendenze...")
    deps_ok, deps_msg = check_dependencies()
    if not deps_ok:
        print(f"❌ {deps_msg}")
        sys.exit(1)
    print("✅ Dipendenze OK")

    # ─── Cleanup orphan files from previous runs ───
    removed = cleanup_orphan_files(download_dir)
    if removed:
        print(f"🧹 Puliti {len(removed)} file orfani da {download_dir}")

    # ─── Initialize whitelist ───
    whitelist = Whitelist(filepath="data/whitelist.json")

    # ─── Create Pyrogram client ───
    app = create_client(api_id, api_hash)

    # ─── Register handlers ───
    register_handlers(app, whitelist, channel_id, owner_id)

    # ─── Start ───
    print("🚀 Userbot avviato e in ascolto...")
    print(f"   Canale destinazione: {channel_id}")
    print(f"   Owner ID: {owner_id}")
    print(f"   Download dir: {download_dir}")
    app.run()


if __name__ == "__main__":
    main()
