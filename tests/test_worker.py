"""Worker routing: per-job target_channel + kind=upload (mocked client, no net)."""

import asyncio
import contextlib

import bot.handlers as h
import pytest
from bot import progress as p
from bot.history import DownloadHistory
from bot.queue import DownloadQueue


class _FakeClient:
    def __init__(self):
        from unittest.mock import AsyncMock, MagicMock
        self.send_message = AsyncMock(return_value=MagicMock())


async def _drain(calls, key, timeout_s=5.0):
    for _ in range(int(timeout_s / 0.05)):
        await asyncio.sleep(0.05)
        if calls.get(key) and h._get_queue().is_empty():
            return True
    return False


@pytest.mark.asyncio
async def test_worker_uses_target_channel(tmp_path, monkeypatch):
    calls = {}

    async def fake_dl(client, status_msg, url, quality, title,
                      channel_id, owner_id, headers=None):
        calls["channel"] = channel_id
        return "ok"

    monkeypatch.setattr(h, "download_and_upload", fake_dl)
    monkeypatch.setattr(h, "_queue", DownloadQueue(filepath=str(tmp_path / "q.json")))
    monkeypatch.setattr(h, "_history", DownloadHistory(filepath=str(tmp_path / "h.json")))
    h._queue.add("https://example.com/v", "720", "T", {}, target_channel="-1009")
    task = asyncio.create_task(h._queue_worker(_FakeClient(), -1000, 111))
    try:
        assert await _drain(calls, "channel")
        assert calls["channel"] == "-1009"
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        p.reset()


@pytest.mark.asyncio
async def test_worker_upload_kind(tmp_path, monkeypatch):
    up = {}
    f = tmp_path / "clip.mp4"
    f.write_bytes(b"\x00" * 1024)

    async def fake_up(client, event, status_msg, filepath, title, channel_id, url=""):
        up["channel"] = channel_id
        up["filepath"] = filepath

    monkeypatch.setattr(h, "_upload_existing", fake_up)
    monkeypatch.setattr(h, "_queue", DownloadQueue(filepath=str(tmp_path / "q.json")))
    monkeypatch.setattr(h, "_history", DownloadHistory(filepath=str(tmp_path / "h.json")))
    h._queue.add("tg://upload/1", "original", "Clip", {},
                 kind="upload", filepath=str(f), target_channel="-1007")
    task = asyncio.create_task(h._queue_worker(_FakeClient(), -1000, 111))
    try:
        assert await _drain(up, "channel")
        assert (up["channel"], up["filepath"]) == ("-1007", str(f))
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        p.reset()


@pytest.mark.asyncio
async def test_worker_unknown_kind_is_history_error(tmp_path, monkeypatch):
    monkeypatch.setattr(h, "_queue", DownloadQueue(filepath=str(tmp_path / "q.json")))
    hist = DownloadHistory(filepath=str(tmp_path / "h.json"))
    monkeypatch.setattr(h, "_history", hist)
    h._queue.add("https://example.com/bogus", "720", "B", {}, kind="bogus")
    task = asyncio.create_task(h._queue_worker(_FakeClient(), -1000, 111))
    try:
        for _ in range(100):
            await asyncio.sleep(0.05)
            entry = hist.get("https://example.com/bogus")
            if entry is not None and h._get_queue().is_empty():
                break
        entry = hist.get("https://example.com/bogus")
        assert entry is not None and entry.get("status") == "error"
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        p.reset()
