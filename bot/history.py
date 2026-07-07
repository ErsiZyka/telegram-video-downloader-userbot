"""Persistent download history. Tracks which URLs were processed and their outcome.

Saved as JSON in data/download_history.json so the bot remembers across restarts.
"""

import json
import os
import tempfile
import time


class DownloadHistory:
    """Persistent store of download results keyed by URL."""

    def __init__(self, filepath: str = "data/download_history.json"):
        self.filepath = filepath
        self._ensure_file()
        self._data: dict[str, dict] = self._load()

    def _ensure_file(self) -> None:
        os.makedirs(os.path.dirname(self.filepath), exist_ok=True)
        if not os.path.exists(self.filepath):
            self._save({})

    def _load(self) -> dict[str, dict]:
        try:
            with open(self.filepath, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, FileNotFoundError, PermissionError, OSError):
            return {}

    def _save(self, data: dict[str, dict]) -> None:
        dirname = os.path.dirname(self.filepath)
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".json", dir=dirname, delete=False, encoding="utf-8",
            ) as tmp:
                json.dump(data, tmp, indent=2, ensure_ascii=False)
                tmp_path = tmp.name
            os.replace(tmp_path, self.filepath)
            tmp_path = None  # success, no cleanup needed
        except (PermissionError, OSError) as e:
            # Clean up temp file if it was created, then bail
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass
            self._data = data  # keep in memory even if save fails

    def get(self, url: str) -> dict | None:
        """Return history entry for a URL, or None."""
        return self._data.get(url)

    def set_success(self, url: str, filepath: str, title: str) -> None:
        """Record a successful download."""
        self._data[url] = {
            "status": "ok",
            "filepath": filepath,
            "title": title,
            "ts": time.time(),
        }
        self._save(self._data)

    def set_error(self, url: str, error: str, title: str = "") -> None:
        """Record a failed download."""
        self._data[url] = {
            "status": "error",
            "error": error,
            "title": title,
            "ts": time.time(),
        }
        self._save(self._data)

    def remove(self, url: str) -> None:
        """Forget about a URL."""
        if url in self._data:
            del self._data[url]
            self._save(self._data)

    def clear_orphan_entries(self, download_dir: str = "downloads") -> int:
        """Remove history entries whose file no longer exists. Returns count removed."""
        removed = 0
        for url, entry in list(self._data.items()):
            if entry.get("status") == "ok":
                fpath = entry.get("filepath", "")
                if fpath and not os.path.exists(fpath):
                    del self._data[url]
                    removed += 1
        if removed:
            self._save(self._data)
        return removed
