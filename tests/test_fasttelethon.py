"""Regression tests for the Telegram upload transport (parallel MTProto).

``upload_file`` streams the file in 512KB parts through ParallelTransferrer
(one TCP connection per sender). These tests patch the transferrer — no
network, no Telegram session — and verify chunking + progress reporting.
"""

import pytest
from bot import fasttelethon as ft


class _FakeTransferrer:
    """Stand-in for ParallelTransferrer: records parts, uploads nothing."""

    def __init__(self, client, dc_id=None):
        self.client = client
        self.part_size = 0
        self.uploaded: list[bytes] = []

    async def init_upload(self, file_id, file_size, part_size_kb=None,
                          connection_count=None):
        self.part_size = int((part_size_kb or 512) * 1024)
        part_count = (file_size + self.part_size - 1) // self.part_size
        return self.part_size, part_count, file_size > 10 * 1024 * 1024

    async def upload(self, part: bytes):
        self.uploaded.append(bytes(part))

    async def finish_upload(self):
        return None


class _DummyClient:
    """Placeholder client: never touched, ParallelTransferrer is patched."""


@pytest.mark.asyncio
async def test_upload_sends_512kb_parts_with_progress(tmp_path, monkeypatch):
    """The stream is cut into 512KB MTProto parts; progress ends at 100%."""
    part_size = 512 * 1024
    video = tmp_path / "video.mp4"
    video.write_bytes(b"x" * (part_size * 5))
    progress = []
    created = {}

    def _factory(client, dc_id=None):
        transferrer = _FakeTransferrer(client, dc_id)
        created["t"] = transferrer
        return transferrer

    monkeypatch.setattr(ft, "ParallelTransferrer", _factory)

    async def on_progress(sent, total):
        progress.append((sent, total))

    with video.open("rb") as source:
        result = await ft.upload_file(
            _DummyClient(),  # type: ignore[arg-type]
            source,
            progress_callback=on_progress,
        )

    transferrer = created["t"]
    assert [len(part) for part in transferrer.uploaded] == [part_size] * 5
    assert progress[-1] == (part_size * 5, part_size * 5)
    assert [sent for sent, _ in progress] == sorted(sent for sent, _ in progress)
    assert result.parts == 5  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_upload_last_part_may_be_short(tmp_path, monkeypatch):
    """A file that is not a multiple of 512KB ends with a short part."""
    part_size = 512 * 1024
    video = tmp_path / "video.mp4"
    video.write_bytes(b"y" * (part_size * 2 + 12345))
    created = {}

    def _factory(client, dc_id=None):
        transferrer = _FakeTransferrer(client, dc_id)
        created["t"] = transferrer
        return transferrer

    monkeypatch.setattr(ft, "ParallelTransferrer", _factory)

    with video.open("rb") as source:
        result = await ft.upload_file(_DummyClient(), source)  # type: ignore[arg-type]

    transferrer = created["t"]
    assert [len(part) for part in transferrer.uploaded] == [part_size, part_size, 12345]
    assert result.parts == 3  # type: ignore[attr-defined]
