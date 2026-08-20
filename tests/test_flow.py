"""End-to-end flow simulation: verifies the link→menu→number→download state machine.

Mocks Telethon event objects so we can test the handlers logic without a live
Telegram session.
"""
import sys
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, ".")
import bot.handlers as h


class FakeEvent:
    """Minimal stand-in for telethon events.NewMessage."""
    def __init__(self, text, user_id=111222333):
        self.message = MagicMock()
        self.message.text = text
        self.message.id = id(self)
        self.message.reply = AsyncMock(return_value=self.message)
        self.message.edit = AsyncMock()
        self.message.delete = AsyncMock()
        self._sender_id = user_id
        self.is_private = True

    @property
    def sender_id(self):
        return self._sender_id

    async def reply(self, text):
        return await self.message.reply(text)


def make_whitelist(user_id=111222333):
    wl = MagicMock()
    wl.is_authorized.return_value = True
    return wl


@pytest.fixture(autouse=True)
def reset_state():
    h._pending.clear()
    h._cancel_requested = False
    h._edit_muted_until = 0.0
    for attr in ("_current_item",):
        if hasattr(h, attr):
            setattr(h, attr, None)
    if hasattr(h, "_queue") and h._queue is not None:
        h._queue.clear()
    yield
    h._pending.clear()
    for attr in ("_current_item",):
        if hasattr(h, attr):
            setattr(h, attr, None)
    if hasattr(h, "_queue") and h._queue is not None:
        h._queue.clear()


def test_history_filepath_not_corrupted():
    """Regression: _get_history() must use the correct filepath."""
    hist = h._get_history()
    assert hist.filepath == "data/download_history.json"


def test_upload_existing_accepts_url():
    """Regression: _upload_existing must accept a url parameter."""
    import inspect
    sig = inspect.signature(h._upload_existing)
    assert "url" in sig.parameters


@pytest.mark.asyncio
class TestFlow:
    async def test_link_sets_quality_pending(self):
        user_id = 111222333
        event = FakeEvent("https://youtube.com/watch?v=test", user_id=user_id)
        client = MagicMock()
        wl = make_whitelist(user_id)
        with patch("bot.handlers.extract_info", return_value={"title": "Test", "duration": 100}):
            await h.on_message(client, event, wl, channel_id=-100, owner_id=user_id)
        pending = h._get_pending(user_id)
        assert pending is not None
        assert pending["type"] == "quality"
        assert pending["url"] == "https://youtube.com/watch?v=test"

    async def test_number_without_pending_ignored(self):
        user_id = 111222333
        event = FakeEvent("2", user_id=user_id)
        client = MagicMock()
        wl = make_whitelist(user_id)
        with patch("bot.handlers.download_video") as mock_dl:
            await h.on_message(client, event, wl, channel_id=-100, owner_id=user_id)
            assert not mock_dl.called

    async def test_new_link_clears_old_pending(self):
        user_id = 111222333
        h._set_pending(user_id, {"type": "quality", "url": "https://old", "title": "old"})
        event = FakeEvent("https://new.url", user_id=user_id)
        client = MagicMock()
        wl = make_whitelist(user_id)
        with patch("bot.handlers.extract_info", return_value={"title": "New"}):
            await h.on_message(client, event, wl, channel_id=-100, owner_id=user_id)
        pending = h._get_pending(user_id)
        assert pending["url"] == "https://new.url"

    async def test_non_owner_ignored(self):
        event = FakeEvent("https://test.url", user_id=999999)
        client = MagicMock()
        wl = MagicMock()
        wl.is_authorized.return_value = False
        with patch("bot.handlers.extract_info") as mock_extract:
            await h.on_message(client, event, wl, channel_id=-100, owner_id=8415744410)
            assert not mock_extract.called
