"""Telethon client factory."""

from telethon import TelegramClient


def create_client(api_id: int, api_hash: str, session_name: str = "my_video_downloader_bot") -> TelegramClient:
    """
    Create and return a Telethon TelegramClient instance.

    The session file will be stored as '{session_name}.session' in the
    current working directory. cryptg is used for AES-NI acceleration.
    Markdown parse mode is enabled so captions/menu text render bold/italic
    and inline links correctly.
    """
    client = TelegramClient(
        session=session_name,
        api_id=api_id,
        api_hash=api_hash,
    )
    client.parse_mode = "md"
    return client
