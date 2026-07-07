"""Pyrogram client factory."""

from pyrogram import Client


def create_client(api_id: int, api_hash: str, session_name: str = "my_video_downloader_bot") -> Client:
    """
    Create and return a Pyrogram Client instance.

    workers=32 ensures the dispatcher thread pool can handle rapid RPC
    responses during file upload, preventing pipeline stalls.
    """
    return Client(
        name=session_name,
        api_id=api_id,
        api_hash=api_hash,
        workdir=".",
        workers=32,
    )
