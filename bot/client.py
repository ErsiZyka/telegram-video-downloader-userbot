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
    import os
    from bot.logging_config import get_logger
    _log = get_logger("client")
    
    env_session = os.getenv("SESSION_PATH", "").strip() or os.getenv("SESSION_NAME", "").strip()
    if env_session:
        # Prevent cross-OS path conflicts on shared mounts
        is_windows_path = "\\" in env_session or (len(env_session) > 1 and env_session[1] == ":")
        is_linux_path = "/" in env_session
        
        if is_windows_path and os.name != "nt":
            _log.warning("Ignoro SESSION_PATH di Windows su Linux: %s", env_session)
        elif is_linux_path and os.name == "nt":
            _log.warning("Ignoro SESSION_PATH di Linux su Windows: %s", env_session)
        else:
            session_name = env_session

    client = TelegramClient(
        session=session_name,
        api_id=api_id,
        api_hash=api_hash,
    )
    client.parse_mode = "md"
    return client
