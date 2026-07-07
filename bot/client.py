"""Pyrogram client factory."""

from pyrogram import Client


def create_client(api_id: int, api_hash: str, session_name: str = "my_video_downloader_bot") -> Client:
    """
    Create and return a Pyrogram Client instance.

    The session file will be stored as '{session_name}.session' in the
    current working directory.
    """
    return Client(
        name=session_name,
        api_id=api_id,
        api_hash=api_hash,
        workdir=".",
    )
