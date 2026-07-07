import json
import os
import tempfile
from datetime import datetime, timezone


class Whitelist:
    """Persistent whitelist of authorized Telegram user IDs, stored as JSON."""

    def __init__(self, filepath: str = "data/whitelist.json"):
        self.filepath = filepath
        self._ensure_file()

    def _ensure_file(self) -> None:
        """Create the JSON file with empty structure if it doesn't exist."""
        os.makedirs(os.path.dirname(self.filepath), exist_ok=True)
        if not os.path.exists(self.filepath):
            self._write({"users": {}})

    def _read(self) -> dict:
        with open(self.filepath, "r", encoding="utf-8") as f:
            return json.load(f)

    def _write(self, data: dict) -> None:
        """Atomic write: write to temp file, then rename."""
        dirname = os.path.dirname(self.filepath)
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".json",
            dir=dirname,
            delete=False,
            encoding="utf-8",
        ) as tmp:
            json.dump(data, tmp, indent=2, ensure_ascii=False)
            tmp_path = tmp.name
        os.replace(tmp_path, self.filepath)

    def is_authorized(self, user_id: int) -> bool:
        data = self._read()
        return str(user_id) in data.get("users", {})

    def add(self, user_id: int, username: str = "", added_by: str = "owner") -> None:
        data = self._read()
        data.setdefault("users", {})[str(user_id)] = {
            "username": username,
            "added_by": added_by,
            "added_at": datetime.now(timezone.utc).isoformat(),
        }
        self._write(data)

    def remove(self, user_id: int) -> bool:
        data = self._read()
        key = str(user_id)
        if key in data.get("users", {}):
            del data["users"][key]
            self._write(data)
            return True
        return False

    def get_all(self) -> dict[int, dict]:
        data = self._read()
        return {int(k): v for k, v in data.get("users", {}).items()}
