"""Persistent download queue. Stores pending downloads across restarts.

Separate from history: this is the "to-do" list, history is the "done" log.
Uses atomic writes (tempfile + os.replace) like DownloadHistory.

Crash-safe semantics: the worker uses peek()+remove() (NOT pop()), so an
item being processed is only removed from the queue once download+upload
fully completes. If the bot crashes mid-download, the item stays in the
queue and is reprocessed on the next startup (yt-dlp resumes .part files).
"""

import json
import os
import tempfile
import time


class DownloadQueue:
    """FIFO queue of pending downloads, persisted to a JSON file."""

    def __init__(self, filepath: str = "data/queue.json"):
        self.filepath = filepath
        self._ensure_file()
        self._items: list[dict] = self._load()

    def _ensure_file(self) -> None:
        os.makedirs(os.path.dirname(self.filepath) or ".", exist_ok=True)
        if not os.path.exists(self.filepath):
            self._save([])

    def _load(self) -> list[dict]:
        try:
            with open(self.filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
        except (json.JSONDecodeError, FileNotFoundError, PermissionError, OSError):
            return []

    def _save(self, items: list[dict]) -> None:
        dirname = os.path.dirname(self.filepath) or "."
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".json", dir=dirname, delete=False, encoding="utf-8",
            ) as tmp:
                json.dump(items, tmp, indent=2, ensure_ascii=False)
                tmp_path = tmp.name
            os.replace(tmp_path, self.filepath)
            tmp_path = None
        except (PermissionError, OSError):
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass
            self._items = items

    def add(self, url: str, quality: str, title: str) -> int:
        """Append an item. Returns its 1-based position in the queue."""
        self._items.append({
            "url": url, "quality": quality, "title": title, "ts": time.time(),
        })
        self._save(self._items)
        return len(self._items)

    def peek(self) -> dict | None:
        """Return the first item WITHOUT removing it (crash-safe)."""
        return self._items[0] if self._items else None

    def remove(self, url: str) -> bool:
        """Remove the item with this url. Returns True if found and removed."""
        for i, item in enumerate(self._items):
            if item.get("url") == url:
                del self._items[i]
                self._save(self._items)
                return True
        return False

    def move_front(self, url: str) -> bool:
        """Move the item with this url to the front. Returns True if found.

        No-op (returns True) if it is already first. Does NOT interrupt an
        in-progress download (the worker holds _current_item separately); the
        moved item simply becomes the next one processed.
        """
        for i, item in enumerate(self._items):
            if item.get("url") == url:
                if i == 0:
                    return True
                self._items.insert(0, self._items.pop(i))
                self._save(self._items)
                return True
        return False

    def clear(self) -> int:
        """Empty the queue. Returns the number of items removed."""
        n = len(self._items)
        self._items = []
        self._save(self._items)
        return n

    @property
    def items(self) -> list[dict]:
        return list(self._items)

    def is_empty(self) -> bool:
        return len(self._items) == 0
