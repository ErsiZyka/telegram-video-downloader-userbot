"""Tests for the local control API (no network beyond localhost, no Telegram).

The API fixture points the handlers' queue singleton at a tmp dir so the
live bot's queue file is never touched.
"""

import json
import threading
import urllib.error
import urllib.request

import pytest

import bot.handlers as h
from bot import progress as p
from bot.api import create_server
from bot.queue import DownloadQueue

VALID_CRED = "test-token"


def _call(server, method, path, body=None, cred: str | None = VALID_CRED):
    url = f"http://127.0.0.1:{server.server_port}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)  # pi-lens-ignore: S310
    if cred is not None:
        req.add_header("Authorization", f"Bearer {cred}")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:  # pi-lens-ignore: S310
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


@pytest.fixture()
def api(tmp_path, monkeypatch):
    monkeypatch.setattr(h, "_queue", DownloadQueue(filepath=str(tmp_path / "q.json")))
    monkeypatch.setattr(h, "_current_item", None)
    monkeypatch.setattr(h, "_cancel_requested", False)
    server = create_server("127.0.0.1", 0, VALID_CRED)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    thread.join(timeout=5)
    p.reset()


def test_health_requires_auth(api):
    _, body = _call(api, "GET", "/api/health", cred=None)
    assert body["ok"] is False
    status, body = _call(api, "GET", "/api/health", cred="wrong")
    assert status == 401
    status, body = _call(api, "GET", "/api/health")
    assert status == 200 and body["ok"] is True


def test_queue_add_and_dedupe(api):
    status, body = _call(api, "POST", "/api/queue",
                         {"url": "https://example.com/v/1", "quality": "720",
                          "title": "V1"})
    assert (status, body["position"]) == (200, 1)
    status, body = _call(api, "POST", "/api/queue",
                         {"url": "https://example.com/v/1", "quality": "720"})
    assert status == 409
    status, _ = _call(api, "POST", "/api/queue", {"url": "not-a-url"})
    assert status == 400
    status, _ = _call(api, "POST", "/api/queue",
                      {"url": "https://example.com/v/2", "quality": "8k"})
    assert status == 400
    status, body = _call(api, "GET", "/api/queue")
    assert status == 200 and len(body["items"]) == 1


def test_cancel_job_drops_queued_item(api):
    _call(api, "POST", "/api/queue",
          {"url": "https://example.com/v/9", "quality": "720"})
    status, body = _call(api, "POST", "/api/cancel",
                         {"job": "https://example.com/v/9"})
    assert status == 200 and body["job"] == "https://example.com/v/9"
    assert p.is_cancelled("https://example.com/v/9")
    _, body = _call(api, "GET", "/api/queue")
    assert body["items"] == []


def test_history_missing_is_404(api):
    status, body = _call(api, "GET", "/api/history?url=https://example.com/nope")
    # Deve essere il 404 dell'endpoint (entry assente), non dell'endpoint mancante.
    assert status == 404 and body["error"] == "not found"


def test_queue_add_passthrough_extras(api):
    status, body = _call(api, "POST", "/api/queue",
                         {"url": "https://example.com/x", "quality": "720",
                          "target_channel": "-1001", "kind": "web"})
    assert status == 200
    _, q = _call(api, "GET", "/api/queue")
    assert q["items"][0]["target_channel"] == "-1001"
    assert q["items"][0]["kind"] == "web"


def test_move_and_clear(api):
    _call(api, "POST", "/api/queue", {"url": "https://example.com/a"})
    _call(api, "POST", "/api/queue", {"url": "https://example.com/b"})
    status, body = _call(api, "POST", "/api/move", {"url": "https://example.com/b"})
    assert (status, body["moved"]) == (200, True)
    _, q = _call(api, "GET", "/api/queue")
    assert q["items"][0]["url"] == "https://example.com/b"
    status, body = _call(api, "POST", "/api/move", {"url": "https://example.com/zz"})
    assert (status, body["moved"]) == (404, False)
    status, body = _call(api, "POST", "/api/clear", {})
    assert (status, body["removed"]) == (200, 2)
    _, q = _call(api, "GET", "/api/queue")
    assert q["items"] == []


def test_analyze_single_playlist_error(api, monkeypatch):
    import bot.api as api_mod
    monkeypatch.setattr(api_mod.h, "extract_info",
                        lambda url: {"title": "T", "duration": 60,
                                     "thumbnail": "http://t/x.jpg",
                                     "webpage_url": url, "uploader": "u",
                                     "filesize_approx": 10})
    status, body = _call(api, "POST", "/api/analyze", {"url": "https://example.com/v"})
    assert (status, body["kind"], body["title"]) == (200, "single", "T")
    assert body["direct_url"] == "https://example.com/v"
    monkeypatch.setattr(api_mod.h, "extract_info",
                        lambda url: [{"title": "A", "webpage_url": "https://example.com/a"}])
    status, body = _call(api, "POST", "/api/analyze", {"url": "https://example.com/pl"})
    assert (status, body["kind"]) == (200, "playlist")
    assert body["videos"][0]["webpage_url"] == "https://example.com/a"
    status, _ = _call(api, "POST", "/api/analyze", {"url": "not-a-url"})
    assert status == 400


def test_analyze_extract_error(api, monkeypatch):
    import bot.api as api_mod
    from bot.downloader import ExtractError
    def _boom(url):
        raise ExtractError("nope")
    monkeypatch.setattr(api_mod.h, "extract_info", _boom)
    status, body = _call(api, "POST", "/api/analyze", {"url": "https://example.com/v"})
    assert status == 200 and body["ok"] is False


def test_history_recent(api):
    hist = h._get_history()
    hist.set_success("https://example.com/old", "f.mp4", "Old")
    hist.set_error("https://example.com/new", "boom", "New")
    try:
        status, body = _call(api, "GET", "/api/history/recent?limit=10")
        assert status == 200
        urls = [e["url"] for e in body["entries"]]
        assert urls[0] == "https://example.com/new"
        assert "https://example.com/old" in urls
    finally:
        hist.remove("https://example.com/old")
        hist.remove("https://example.com/new")
