"""Video site webapp (LAN only, secret-prefix auth).

Browser-facing frontend for the Telegram video downloader. Talks to the bot
EXCLUSIVELY through its localhost Local API (`bot/api.py`): it never opens
the Telethon session and never reads/writes queue/history files.

Run: ``venv/bin/python -m uvicorn webapp:app --host 0.0.0.0 --port 8090``
"""

from __future__ import annotations

import os
import re
import secrets
import time
import contextlib

import requests
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from starlette.datastructures import UploadFile

from bot.downloader import is_over_limit

load_dotenv()
HERE = os.path.dirname(os.path.abspath(__file__))
try:
    os.chdir(HERE)
except OSError as e:
    print(f"WARN: chdir {HERE} fallito ({e})")

app = FastAPI(title="VideoSite", docs_url=None, redoc_url=None, openapi_url=None)

try:
    SITE_PORT = int(os.getenv("SITE_PORT", "8090") or 8090)
except ValueError:
    SITE_PORT = 8090


def _site_token() -> str:
    tok = os.getenv("SITE_TOKEN", "").strip()
    if tok:
        return tok
    tok = secrets.token_urlsafe(32)
    try:
        with open(".env", "a", encoding="utf-8") as f:
            f.write(f"\nSITE_TOKEN={tok}\n")
    except OSError:
        pass
    return tok


SITE_TOKEN = _site_token()
PREFIX = f"/s/{SITE_TOKEN}"
CHANNELS_FILE = "data/channels.json"

CHANNEL_RE = re.compile(r"^(?:@[A-Za-z0-9_]{5,}|-100\d+|-\d+)$")


def valid_channel(value: str | None) -> bool:
    """True for @username / -100id / -id channel references."""
    return bool(CHANNEL_RE.match((value or "").strip()))


class ChannelsStore:
    """Webapp-owned channel registry (the bot never touches this file)."""

    def __init__(self, filepath: str = CHANNELS_FILE):
        self.filepath = filepath
        self._items: list[dict] = self._load()
        if not self._items:
            seed = (os.getenv("CHANNEL_ID", "") or "").strip()
            if seed:
                self._items = [{"id": seed, "label": "Predefinito",
                                "added_ts": time.time()}]
                self._save()

    def _load(self) -> list[dict]:
        try:
            with open(self.filepath, encoding="utf-8") as f:
                data = __import__("json").load(f)
            return list(data) if isinstance(data, list) else []
        except (OSError, ValueError):
            return []

    def _save(self) -> None:
        import json
        import tempfile

        try:
            os.makedirs(os.path.dirname(self.filepath) or ".", exist_ok=True)
        except OSError:
            return
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".json",
                dir=os.path.dirname(self.filepath) or ".",
                delete=False, encoding="utf-8",
            ) as tmp:
                json.dump(self._items, tmp, indent=2, ensure_ascii=False)
                tmp_path = tmp.name
            os.replace(tmp_path, self.filepath)
        finally:
            if tmp_path and os.path.exists(tmp_path):
                with contextlib.suppress(OSError):
                    os.unlink(tmp_path)

    @property
    def items(self) -> list[dict]:
        return [dict(e) for e in self._items]

    def add(self, cid: str, label: str | None = None) -> dict:
        for e in self._items:
            if e.get("id") == cid:
                return dict(e)
        entry = {"id": cid, "label": (label or cid)[:80], "added_ts": time.time()}
        self._items.append(entry)
        self._save()
        return dict(entry)

    def rename(self, cid: str, label: str) -> bool:
        for e in self._items:
            if e.get("id") == cid:
                e["label"] = (label or cid)[:80]
                self._save()
                return True
        return False

    def remove(self, cid: str) -> bool:
        """Remove a channel; refuses to delete the last remaining one."""
        if len(self._items) <= 1 and any(e.get("id") == cid for e in self._items):
            return False
        for i, e in enumerate(self._items):
            if e.get("id") == cid:
                del self._items[i]
                self._save()
                return True
        return False


_CHANNELS: ChannelsStore | None = None


def _get_channels() -> ChannelsStore:
    global _CHANNELS
    if _CHANNELS is None or _CHANNELS.filepath != CHANNELS_FILE:
        _CHANNELS = ChannelsStore(filepath=CHANNELS_FILE)
    return _CHANNELS


def _local_token() -> str:
    tok = os.getenv("LOCAL_API_TOKEN", "").strip()
    if tok:
        return tok
    try:
        with open(os.path.join("data", ".local_api_token"), encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def _local_api_port() -> int:
    try:
        return int(os.getenv("LOCAL_API_PORT", "8091") or 8091)
    except ValueError:
        return 8091


def _local(method: str, path: str, body: dict | None = None,
           timeout: int = 30) -> tuple[int, dict]:
    """Call the bot's Local API. Raises RuntimeError when the bot is offline."""
    url = f"http://127.0.0.1:{_local_api_port()}{path}"
    try:
        r = requests.request(method, url, json=body,
                             headers={"Authorization": f"Bearer {_local_token()}"},
                             timeout=timeout)
    except requests.RequestException as e:
        raise RuntimeError(f"bot offline: {e}") from e
    try:
        return r.status_code, r.json()
    except ValueError:
        return r.status_code, {"ok": False, "error": "bad gateway payload"}


def _is_allowed(path: str) -> bool:
    return path == PREFIX or path.startswith(PREFIX + "/")


@app.middleware("http")
async def _secret_gate(request: Request, call_next):
    if not _is_allowed(request.url.path):
        return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
    return await call_next(request)


@app.get(PREFIX)
async def _root_redirect():
    return RedirectResponse(PREFIX + "/", status_code=307)


@app.get(PREFIX + "/")
async def _index():
    page = os.path.join(HERE, "templates", "site.html")
    if not os.path.exists(page):
        return JSONResponse({"ok": False, "error": "ui not installed"}, status_code=404)
    return FileResponse(page, media_type="text/html; charset=utf-8")


def _offline() -> JSONResponse:
    return JSONResponse({"ok": False, "error": "bot offline"}, status_code=502)


@app.get(PREFIX + "/api/state")
def _state():
    try:
        _, queue = _local("GET", "/api/queue")
        _, progress = _local("GET", "/api/progress")
        _, hist = _local("GET", "/api/history/recent?limit=10")
    except RuntimeError:
        return _offline()
    return {"ok": True, "bot_online": True,
            "current": queue.get("current"), "items": queue.get("items", []),
            "progress": progress.get("jobs", {}),
            "history": hist.get("entries", []),
            "channels": _get_channels().items}


@app.get(PREFIX + "/api/channels")
def _channels_list():
    return {"ok": True, "channels": _get_channels().items}


@app.post(PREFIX + "/api/channels")
async def _channels_add(request: Request):
    try:
        body = await request.json()
    except ValueError:
        body = {}
    cid = str(body.get("id", "") or "").strip()
    if not valid_channel(cid):
        return JSONResponse({"ok": False, "error": "invalid channel (ID o @username)"},
                            status_code=400)
    store = _get_channels()
    existed = any(e.get("id") == cid for e in store.items)
    entry = store.add(cid, str(body.get("label", "") or "").strip() or None)
    return {"ok": True, "channel": entry, "created": not existed}


@app.put(PREFIX + "/api/channels/{cid}")
async def _channels_rename(cid: str, request: Request):
    try:
        body = await request.json()
    except ValueError:
        body = {}
    label = str(body.get("label", "") or "").strip()
    if not label:
        return JSONResponse({"ok": False, "error": "missing label"}, status_code=400)
    if not _get_channels().rename(cid, label):
        return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
    return {"ok": True}


@app.delete(PREFIX + "/api/channels/{cid}")
def _channels_delete(cid: str):
    if not _get_channels().remove(cid):
        return JSONResponse({"ok": False, "error": "not found or last channel"},
                            status_code=409)
    return {"ok": True}


@app.post(PREFIX + "/api/preview")
async def _preview(request: Request):
    try:
        body = await request.json()
    except ValueError:
        body = {}
    url = str(body.get("url", "") or "").strip()
    if not url.startswith(("http://", "https://")):
        return JSONResponse({"ok": False, "error": "invalid url"}, status_code=400)
    try:
        _, result = _local("POST", "/api/analyze", {"url": url}, timeout=120)
    except RuntimeError:
        return _offline()
    return result


QUALITY_NUMBERS = {"1": "360", "2": "720", "3": "1080", "4": "max"}
QUALITY_VALUES = ("360", "720", "1080", "max")


@app.post(PREFIX + "/api/enqueue")
async def _enqueue(request: Request):
    try:
        body = await request.json()
    except ValueError:
        return JSONResponse({"ok": False, "error": "invalid json"}, status_code=400)
    raw_urls = body.get("urls", [])
    urls = [str(u or "").strip() for u in raw_urls if str(u or "").strip()]
    quality = QUALITY_NUMBERS.get(str(body.get("quality", "720")).strip(),
                                  str(body.get("quality", "720")).strip())
    channel = str(body.get("channel", "") or "").strip() or None
    if not urls:
        return JSONResponse({"ok": False, "error": "no urls"}, status_code=400)
    if quality not in QUALITY_VALUES:
        return JSONResponse({"ok": False, "error": "invalid quality"}, status_code=400)
    if channel is not None and not valid_channel(channel):
        return JSONResponse({"ok": False, "error": "invalid channel"}, status_code=400)
    results = []
    try:
        for u in urls:
            if not u.startswith(("http://", "https://", "tg://")):
                results.append({"url": u, "error": "invalid url"})
                continue
            title = str((body.get("titles") or {}).get(u, "") or "")[:150]
            payload = {"url": u, "quality": quality,
                       "title": title or u[:80], "headers": {}}
            if channel:
                payload["target_channel"] = channel
            code, resp = _local("POST", "/api/queue", payload)
            if code == 200:
                results.append({"url": u, "position": resp.get("position")})
            else:
                results.append({"url": u, "error": resp.get("error", "rejected")})
    except RuntimeError:
        return _offline()
    return {"ok": True, "results": results}


@app.post(PREFIX + "/api/upload")
async def _upload(request: Request):
    form = await request.form()
    upload = form.get("file")
    channel = str(form.get("channel", "") or "").strip() or None
    if not isinstance(upload, UploadFile) or not upload.filename:
        return JSONResponse({"ok": False, "error": "missing file"}, status_code=400)
    if channel is not None and not valid_channel(channel):
        return JSONResponse({"ok": False, "error": "invalid channel"}, status_code=400)
    safe = re.sub(r"[^A-Za-z0-9._ -]", "_", upload.filename).strip(" .")[:100]
    if not safe:
        safe = "upload.bin"
    stamp = time.time_ns()
    dest = os.path.join("downloads", f"upload_{stamp}_{safe}")
    try:
        os.makedirs("downloads", exist_ok=True)
        size = 0
        with open(dest, "wb") as f:
            while True:
                chunk = await upload.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if is_over_limit(size):
                    raise _TooLarge()
                f.write(chunk)
    except _TooLarge:
        with contextlib.suppress(OSError):
            os.unlink(dest)
        return JSONResponse({"ok": False, "error": "file troppo grande"}, status_code=413)
    except OSError as e:
        return JSONResponse({"ok": False, "error": f"write failed: {e}"}, status_code=500)
    payload = {"url": f"tg://upload/{stamp}", "quality": "original",
               "title": safe[:80], "kind": "upload", "filepath": dest}
    if channel:
        payload["target_channel"] = channel
    try:
        code, resp = _local("POST", "/api/queue", payload)
    except RuntimeError:
        return _offline()
    if code != 200:
        return JSONResponse({"ok": False, "error": resp.get("error", "rejected")},
                            status_code=code)
    return {"ok": True, "position": resp.get("position")}


class _TooLarge(Exception):
    pass


@app.post(PREFIX + "/api/priority")
async def _priority(request: Request):
    try:
        body = await request.json()
    except ValueError:
        body = {}
    try:
        code, resp = _local("POST", "/api/move", {"url": body.get("url", "")})
    except RuntimeError:
        return _offline()
    return JSONResponse(resp, status_code=code)


@app.post(PREFIX + "/api/clear")
def _clear():
    try:
        _, resp = _local("POST", "/api/clear", {})
    except RuntimeError:
        return _offline()
    return resp


@app.post(PREFIX + "/api/cancel")
async def _cancel(request: Request):
    try:
        body = await request.json()
    except ValueError:
        body = {}
    try:
        _, resp = _local("POST", "/api/cancel", {"job": body.get("job", "") or ""})
    except RuntimeError:
        return _offline()
    return resp
