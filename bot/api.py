"""Local control API for the video downloader bot (localhost only).

The website / tooling must NEVER open the Telethon session SQLite file
directly (``database is locked``) and must NEVER read/write ``queue.json``
behind the worker's back (lost updates: the in-memory queue is the truth).
All site operations go through this tiny in-process HTTP API, which runs
inside the bot process and touches only the live singletons.

Endpoints (all JSON, all require ``Authorization: Bearer <token>``):
    GET  /api/health    -> {ok, queue_len, current}
    GET  /api/queue     -> {current, items}
    POST /api/queue     -> {url, quality?, title?, headers?, kind?, filepath?,
                       msg_id?, page_url?, target_channel?} adds a job
    POST /api/cancel    -> {job?} cancels one job (or the current one)
    POST /api/move      -> {url} moves a queued job to the front
    POST /api/clear     -> {} empties the queue
    POST /api/analyze   -> {url} previews a link (single/playlist/error)
    GET  /api/progress  -> {current, jobs}
    GET  /api/history?url=... -> history entry for a URL
    GET  /api/history/recent?limit=20 -> latest entries, ts desc

Stdlib only (no new dependencies). Bound to 127.0.0.1 by default.
Disabled with ``LOCAL_API_PORT=0`` / ``off`` / empty.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import os
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import bot.handlers as h
from bot import progress as _progress
from bot.downloader import ExtractError, VideoUnavailableError
from bot.extractors import get_extractor
from bot.logging_config import get_logger

_log = get_logger("api")

def _token_path() -> str:
    """Filesystem location of the persisted API bearer token."""
    return os.path.join("data", ".local_api_token")


def _load_or_create_token() -> str:
    env_token = os.getenv("LOCAL_API_TOKEN", "").strip()
    if env_token:
        return env_token
    try:
        token_path = _token_path()
        if os.path.exists(token_path):
            with open(token_path, encoding="utf-8") as f:
                token = f.read().strip()
            if token:
                return token
    except OSError:
        pass
    token = secrets.token_urlsafe(32)
    try:
        token_path = _token_path()
        os.makedirs(os.path.dirname(token_path) or ".", exist_ok=True)
        with open(token_path, "w", encoding="utf-8") as f:
            f.write(token)
        with contextlib.suppress(OSError):
            os.chmod(token_path, 0o600)
    except OSError:
        pass
    return token


def _check_auth(handler: BaseHTTPRequestHandler, token: str) -> bool:
    auth = handler.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return False
    return hmac.compare_digest(auth[len("Bearer "):], token)


class _Handler(BaseHTTPRequestHandler):
    server_version = "VideoBotAPI/1"

    def log_message(self, format: str, *args) -> None:  # keep stdlib quiet
        _log.debug("api %s", format % args)

    # -- helpers ---------------------------------------------------------
    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _guard(self) -> bool:
        if not _check_auth(self, self.server.api_token):  # type: ignore[attr-defined]
            self._send(401, {"ok": False, "error": "unauthorized"})
            return False
        return True

    def _read_json(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", "0") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > 64 * 1024:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            return {}

    # -- routes ----------------------------------------------------------
    def do_GET(self) -> None:
        if not self._guard():
            return
        parsed = urlparse(self.path)
        if parsed.path == "/api/health":
            queue = h._get_queue()
            self._send(200, {
                "ok": True,
                "queue_len": len(queue.items),
                "current": h._current_item,
            })
        elif parsed.path == "/api/queue":
            queue = h._get_queue()
            self._send(200, {"current": h._current_item, "items": queue.items})
        elif parsed.path == "/api/progress":
            self._send(200, {
                "current": _progress.get_current_job(),
                "jobs": _progress.all_progress(),
            })
        elif parsed.path == "/api/history":
            url = parse_qs(parsed.query).get("url", [""])[0]
            entry = h._get_history().get(url) if url else None
            if entry is None:
                self._send(404, {"ok": False, "error": "not found"})
            else:
                self._send(200, {"ok": True, "entry": entry})
        elif parsed.path == "/api/history/recent":
            try:
                limit = int(parse_qs(parsed.query).get("limit", ["20"])[0])
            except (TypeError, ValueError):
                limit = 20
            limit = max(1, min(limit, 200))
            hist = h._get_history()
            ranked = sorted(hist._data.items(),
                            key=lambda kv: kv[1].get("ts", 0), reverse=True)[:limit]
            self._send(200, {"ok": True, "entries": [
                {"url": url, **entry} for url, entry in ranked]})
            url = parse_qs(parsed.query).get("url", [""])[0]
            entry = h._get_history().get(url) if url else None
            if entry is None:
                self._send(404, {"ok": False, "error": "not found"})
            else:
                self._send(200, {"ok": True, "entry": entry})
        else:
            self._send(404, {"ok": False, "error": "unknown endpoint"})

    def do_POST(self) -> None:
        if not self._guard():
            return
        parsed = urlparse(self.path)
        if parsed.path == "/api/queue":
            self._handle_queue_add()
        elif parsed.path == "/api/analyze":
            self._handle_analyze()
        elif parsed.path == "/api/cancel":
            self._handle_cancel()
        elif parsed.path == "/api/move":
            self._handle_move()
        elif parsed.path == "/api/clear":
            self._handle_clear()
        else:
            self._send(404, {"ok": False, "error": "unknown endpoint"})

    def _handle_queue_add(self) -> None:
        body = self._read_json()
        url = str(body.get("url", "")).strip()
        quality = str(body.get("quality", "720")).strip()
        title = str(body.get("title", "") or "Video")[:150]
        headers = body.get("headers") or {}
        if not url.startswith(("http://", "https://")) and not url.startswith("tg://"):
            self._send(400, {"ok": False, "error": "invalid url"})
            return
        if quality not in h.QUALITY_CHOICES and quality not in ("360", "720", "1080", "max"):
            self._send(400, {"ok": False, "error": "invalid quality"})
            return
        if not isinstance(headers, dict):
            headers = {}
        extra = {k: body[k] for k in (
            "kind", "filepath", "msg_id", "page_url", "target_channel",
        ) if body.get(k) is not None}
        queue = h._get_queue()
        current = h._current_item
        if current is not None and current.get("url") == url:
            self._send(409, {"ok": False, "error": "already in progress"})
            return
        if any(it.get("url") == url for it in queue.items):
            self._send(409, {"ok": False, "error": "already queued"})
            return
        pos = queue.add(url, quality, title, headers, **extra)
        _log.info("API: accodato %s [%s] (pos %d)", title[:60], quality, pos)
        self._send(200, {"ok": True, "position": pos})

    def _handle_analyze(self) -> None:
        body = self._read_json()
        url = str(body.get("url", "") or "").strip()
        if not url.startswith(("http://", "https://")):
            self._send(400, {"ok": False, "error": "invalid url"})
            return
        loop = getattr(self.server, "bot_loop", None)
        try:
            if loop is None:
                # Tests / inline use: drive the coroutine on a fresh loop.
                result = asyncio.run(_do_analyze(url))
            else:
                fut = asyncio.run_coroutine_threadsafe(_do_analyze(url), loop)
                result = fut.result(timeout=240)
        except TimeoutError:
            self._send(504, {"ok": False, "error": "analysis timeout"})
            return
        except Exception as e:
            _log.warning("API analyze fallita: %r", e)
            self._send(500, {"ok": False, "error": "analysis failed"})
            return
        self._send(200, result)

    def _handle_move(self) -> None:
        body = self._read_json()
        url = str(body.get("url", "") or "").strip()
        if not url:
            self._send(400, {"ok": False, "error": "missing url"})
            return
        moved = h._get_queue().move_front(url)
        self._send(200 if moved else 404, {"ok": moved, "moved": moved})

    def _handle_clear(self) -> None:
        removed = h._get_queue().clear()
        _log.info("API: coda svuotata (%d item)", removed)
        self._send(200, {"ok": True, "removed": removed})

    def _handle_cancel(self) -> None:
        body = self._read_json()
        job = str(body.get("job", "") or "").strip()
        if job:
            _progress.request_cancel(job)
            # A queued (not yet running) job is dropped right away so the
            # cancel is visible even before the worker reaches it.
            h._get_queue().remove(job)
            _log.info("API: cancel richiesto per %s", job[:80])
            self._send(200, {"ok": True, "job": job})
        else:
            h._cancel_requested = True
            current = _progress.get_current_job()
            if current:
                _progress.request_cancel(current)
            _log.info("API: cancel richiesto (corrente)")
            self._send(200, {"ok": True, "job": current})


async def _do_analyze(url: str) -> dict:
    """Analyze one link under the bot's extraction lock.

    Single choke point for metadata so the site never hammers sites in
    parallel with the bot (IP rate-limit bans). Dedicated extractor first,
    then yt-dlp — the universal browser fallback stays with the worker.
    """
    extractor = get_extractor(url)
    if extractor is not None:
        try:
            async with h._extract_lock:
                finfo = await extractor.extract(url)
        except Exception as e:
            return {"ok": False, "error": f"extraction failed: {e}"}
        return {"ok": True, "kind": "single",
                "title": finfo.title or "Video", "duration": 0,
                "thumbnail": "", "filesize_approx": 0,
                "direct_url": finfo.url, "headers": finfo.headers or {},
                "fixed_quality": bool(getattr(finfo, "fixed_quality", False)),
                "page_url": url}
    loop = asyncio.get_running_loop()
    try:
        async with h._extract_lock:
            info = await loop.run_in_executor(None, h.extract_info, url)
    except (ExtractError, VideoUnavailableError) as e:
        return {"ok": False, "error": str(e)}
    if isinstance(info, list):
        return {"ok": True, "kind": "playlist", "url": url,
                "videos": [{"title": v.get("title", "Sconosciuto"),
                              "webpage_url": v.get("webpage_url", "")}
                             for v in info]}
    return {"ok": True, "kind": "single",
            "title": info.get("title", "Sconosciuto"),
            "duration": info.get("duration", 0),
            "thumbnail": info.get("thumbnail", ""),
            "filesize_approx": info.get("filesize_approx", 0),
            "direct_url": url, "headers": {}, "fixed_quality": False,
            "page_url": url}


def create_server(host: str = "127.0.0.1", port: int = 0,
                  token: str = "", loop=None) -> ThreadingHTTPServer:
    """Build (not yet serving) the API server. ``port=0`` = ephemeral."""
    server = ThreadingHTTPServer((host, port), _Handler)
    server.api_token = token or _load_or_create_token()  # type: ignore[attr-defined]
    server.bot_loop = loop  # type: ignore[attr-defined]
    server.daemon_threads = True
    return server


def start_local_api(loop=None) -> ThreadingHTTPServer | None:
    """Start the API in a daemon thread from env config. None = disabled."""
    raw_port = os.getenv("LOCAL_API_PORT", "8091").strip().lower()
    if raw_port in ("", "0", "off", "no", "false"):
        return None
    host = os.getenv("LOCAL_API_HOST", "127.0.0.1").strip() or "127.0.0.1"
    try:
        port = int(raw_port)
    except ValueError:
        _log.warning("LOCAL_API_PORT non valido: %r (API disattivata)", raw_port)
        return None
    # The API must never listen outside localhost: it runs shell-adjacent
    # queue operations with a static bearer token (no user accounts).
    if host not in ("127.0.0.1", "localhost", "::1"):
        _log.warning("LOCAL_API_HOST %r non-loopback: forzo 127.0.0.1", host)
        host = "127.0.0.1"
    try:
        server = create_server(host, port, "", loop)
    except OSError as e:
        _log.warning("API locale non avviata (%r)", e)
        return None
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.5},
        name="local-api", daemon=True,
    )
    thread.start()
    digest = hashlib.sha256(server.api_token.encode()).hexdigest()[:12]  # type: ignore[attr-defined]
    _log.info("API locale su http://%s:%d (token sha256:%s…)",
              host, server.server_port, digest)
    return server
