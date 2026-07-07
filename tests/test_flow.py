"""End-to-end flow simulation: verifies the link→menu→number→download state machine.

Mocks Pyrogram Message/Client so we can test the handlers logic without a live
Telegram session. This catches logic regressions before the user runs it live.
"""
import sys
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, ".")
import bot.handlers as h


def test_history_filepath_not_corrupted():
    """Regression: _get_history() must use the correct filepath (not corrupted by sed)."""
    hist = h._get_history()
    assert hist.filepath == "data/download_history.json", \
        f"filepath corrotto: {hist.filepath}"


def test_upload_existing_accepts_url():
    """Regression: _upload_existing must accept a url parameter."""
    import inspect
    sig = inspect.signature(h._upload_existing)
    assert "url" in sig.parameters


class FakeMessage:
    """Minimal stand-in for pyrogram.types.Message."""
    def __init__(self, text, user_id=111222333, chat_id=123):
        self.text = text
        self.id = id(self)
        self.from_user = MagicMock(id=user_id)
        self.chat = MagicMock(id=chat_id)
        self.reply_text = AsyncMock(return_value=self)
        self.edit_text = AsyncMock()
        self.delete = AsyncMock()
        self.reply_to_message_id = None

    def __await__(self):  # so reply_text return value works
        async def _self(): return self
        return _self().__await__()


def make_whitelist(user_id):
    wl = MagicMock()
    wl.is_authorized.return_value = True
    return wl


@pytest.fixture(autouse=True)
def reset_state():
    h._pending.clear()
    h._is_downloading = False
    yield
    h._pending.clear()
    h._is_downloading = False


@pytest.mark.asyncio
class TestFlow:
    async def test_link_sets_quality_pending(self):
        """Sending a link should extract info and set a quality pending state."""
        user_id = 111222333
        msg = FakeMessage("https://youtube.com/watch?v=test", user_id=user_id)
        client = MagicMock()
        wl = make_whitelist(user_id)

        fake_info = {"title": "Test Video", "duration": 100, "uploader": "U"}
        with patch("bot.handlers.extract_info", return_value=fake_info):
            await h.on_message(client, msg, wl, channel_id=-100, owner_id=user_id)

        # After a link, pending state should be a quality menu
        pending = h._get_pending(user_id)
        assert pending is not None
        assert pending["type"] == "quality"
        assert pending["url"] == "https://youtube.com/watch?v=test"
        assert pending["title"] == "Test Video"

    async def test_number_after_link_triggers_download(self):
        """After a link, sending '2' should trigger download with 720p."""
        user_id = 111222333
        h._set_pending(user_id, {
            "type": "quality", "url": "https://test.url", "title": "Test",
        })
        msg = FakeMessage("2", user_id=user_id)
        client = MagicMock()
        client.send_video = AsyncMock()
        wl = make_whitelist(user_id)

        with patch("bot.handlers.download_video", return_value="downloads/x.mp4") as mock_dl, \
             patch("os.path.getsize", return_value=1024 * 1024), \
             patch("os.path.exists", return_value=True), \
             patch("os.remove"):
            await h.on_message(client, msg, wl, channel_id=-100, owner_id=user_id)
            assert mock_dl.called
            args = mock_dl.call_args[0]
            assert args[0] == "https://test.url"
            assert args[1] == "720"  # quality 2 = 720

    async def test_plaintext_number_without_pending_is_ignored(self):
        """A number with no pending menu must NOT crash and must not download."""
        user_id = 111222333
        msg = FakeMessage("2", user_id=user_id)
        client = MagicMock()
        client.send_video = AsyncMock()
        wl = make_whitelist(user_id)

        with patch("bot.handlers.download_video") as mock_dl:
            await h.on_message(client, msg, wl, channel_id=-100, owner_id=user_id)
            assert not mock_dl.called  # nothing downloaded

    async def test_new_link_clears_old_pending(self):
        """A new link should replace any old pending state."""
        user_id = 111222333
        h._set_pending(user_id, {"type": "quality", "url": "https://old", "title": "old"})
        msg = FakeMessage("https://new.url", user_id=user_id)
        client = MagicMock()
        wl = make_whitelist(user_id)

        with patch("bot.handlers.extract_info", return_value={"title": "New"}):
            await h.on_message(client, msg, wl, channel_id=-100, owner_id=user_id)

        pending = h._get_pending(user_id)
        assert pending["url"] == "https://new.url"

    async def test_invalid_quality_prompts_again(self):
        """Sending '9' (invalid) should ask again, not crash."""
        user_id = 111222333
        h._set_pending(user_id, {"type": "quality", "url": "https://x", "title": "x"})
        msg = FakeMessage("9", user_id=user_id)
        client = MagicMock()
        wl = make_whitelist(user_id)

        with patch("bot.handlers.download_video") as mock_dl:
            await h.on_message(client, msg, wl, channel_id=-100, owner_id=user_id)
            assert not mock_dl.called
            # pending should still be there (not cleared)
            assert h._get_pending(user_id) is not None

    async def test_playlist_link_sets_playlist_pending(self):
        """A playlist link sets a playlist pending state."""
        user_id = 111222333
        msg = FakeMessage("https://youtube.com/playlist?list=x", user_id=user_id)
        client = MagicMock()
        wl = make_whitelist(user_id)

        fake_videos = [
            {"title": "V1", "webpage_url": "https://v1", "duration": 60},
            {"title": "V2", "webpage_url": "https://v2", "duration": 120},
        ]
        with patch("bot.handlers.extract_info", return_value=fake_videos):
            await h.on_message(client, msg, wl, channel_id=-100, owner_id=user_id)

        pending = h._get_pending(user_id)
        assert pending["type"] == "playlist"
        assert len(pending["videos"]) == 2

    async def test_non_owner_non_whitelisted_ignored(self):
        """A stranger's message must be ignored (no reply, no processing)."""
        msg = FakeMessage("https://test.url", user_id=999999)
        client = MagicMock()
        wl = MagicMock()
        wl.is_authorized.return_value = False

        with patch("bot.handlers.extract_info") as mock_extract:
            await h.on_message(client, msg, wl, channel_id=-100, owner_id=111222333)
            assert not mock_extract.called
            assert not msg.reply_text.called
