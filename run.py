#!/usr/bin/env python3
"""Telegram Video Downloader Userbot — Entry Point (Telethon)."""

import os
import sys
import asyncio

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from dotenv import load_dotenv

from bot.client import create_client
from bot.whitelist import Whitelist
from bot.downloader import check_dependencies
from bot.history import DownloadHistory


def _load_config() -> dict:
    api_id_str = os.getenv("API_ID", "")
    api_hash = os.getenv("API_HASH", "")
    channel_id_str = os.getenv("CHANNEL_ID") or os.getenv("CHANNEL_USERNAME", "")
    owner_id_str = os.getenv("OWNER_ID", "")

    errors = []
    if not api_id_str:
        errors.append("API_ID non impostato")
    if not api_hash:
        errors.append("API_HASH non impostato")
    if not channel_id_str:
        errors.append("CHANNEL_ID non impostato")
    if not owner_id_str:
        errors.append("OWNER_ID non impostato")

    if errors:
        print("ERRORI DI CONFIGURAZIONE:")
        for e in errors:
            print(f"  - {e}")
        sys.exit(1)

    try:
        api_id = int(api_id_str)
        owner_id = int(owner_id_str)
        channel_id = channel_id_str if channel_id_str.startswith("@") else int(channel_id_str)
    except ValueError as e:
        print(f"Valore numerico non valido: {e}")
        sys.exit(1)

    return {
        "api_id": api_id, "api_hash": api_hash, "channel_id": channel_id,
        "owner_id": owner_id, "download_dir": os.getenv("DOWNLOAD_DIR", "downloads/"),
    }


async def main() -> None:
    load_dotenv()
    config = _load_config()
    download_dir = config["download_dir"]
    os.makedirs(download_dir, exist_ok=True)

    print("Verifica dipendenze...")
    deps_ok, deps_msg = check_dependencies()
    if not deps_ok:
        print(f"ERRORE: {deps_msg}")
        sys.exit(1)
    print("Dipendenze OK")

    history = DownloadHistory(filepath="data/download_history.json")
    removed_resolved = history.purge_resolved_errors()
    if removed_resolved:
        print(f"Pulite {removed_resolved} entry di errore già risolte dalla cronologia")
    removed_h = history.clear_orphan_entries(download_dir)
    if removed_h:
        print(f"Pulite {removed_h} entry orfane dalla cronologia")

    whitelist = Whitelist(filepath="data/whitelist.json")
    client = create_client(config["api_id"], config["api_hash"])

    from bot.handlers import register_handlers
    register_handlers(client, whitelist, config["channel_id"], config["owner_id"])

    await client.start()
    print("Userbot avviato e in ascolto...")
    print(f"   Canale: {config['channel_id']}")
    print(f"   Owner ID: {config['owner_id']}")

    try:
        print("Preparazione cache peer...")
        async for _ in client.iter_dialogs():
            pass
        print("Cache peer popolata.")
    except Exception as e:
        print(f"ATTENZIONE: dialoghi non letti ({e})")

    try:
        entity = await client.get_entity(config["channel_id"])
        name = getattr(entity, "title", config["channel_id"])
        print(f"Canale risolto: {name}")
    except Exception as e:
        print(f"ATTENZIONE: canale non risolto ({e})")

    # Start the download queue worker (handles resume prompt if queue non-empty)
    from bot.handlers import start_queue_worker
    await start_queue_worker(client, config["channel_id"], config["owner_id"])

    await client.run_until_disconnected()
    print("Userbot fermato.")


if __name__ == "__main__":
    asyncio.run(main())
