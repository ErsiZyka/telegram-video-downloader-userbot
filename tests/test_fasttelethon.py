"""Regression tests for the Telegram upload transport."""

import pytest

from bot import fasttelethon as ft


@pytest.mark.asyncio
async def test_upload_pipelines_four_512kb_parts_before_waiting(tmp_path):
    """Four in-flight MTProto parts keep a healthy high-latency route saturated."""
    part_size = 512 * 1024
    video = tmp_path / "video.mp4"
    video.write_bytes(b"x" * (part_size * 5))
    progress = []

    class Client:
        def __init__(self):
            self.batches = []

        async def __call__(self, requests):
            self.batches.append(list(requests))
            return [True] * len(requests)

    async def on_progress(sent, total):
        progress.append((sent, total))

    client = Client()
    with video.open("rb") as source:
        result = await ft.upload_file(client, source, progress_callback=on_progress)

    assert [len(batch) for batch in client.batches] == [4, 1]
    assert [request.file_part for batch in client.batches for request in batch] == [0, 1, 2, 3, 4]
    assert progress == [(part_size * 4, part_size * 5), (part_size * 5, part_size * 5)]
    assert result.parts == 5
