"""Webapp tests: pure logic + in-process ASGI calls (no network, no httpx).

SITE_TOKEN is pinned before importing webapp so the import never touches
the real .env. The channels store is redirected to tmp in every test.
"""

import json
import os

os.environ.setdefault("SITE_TOKEN", "test-site-token")

import pytest
import webapp
from webapp import ChannelsStore, valid_channel


async def _asgi(app, method, path, body=None):
    raw = json.dumps(body).encode() if body is not None else b""
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
             "method": method, "scheme": "http", "path": path,
             "raw_path": path.encode(), "query_string": b"", "root_path": "",
             "headers": [(b"content-type", b"application/json")] if body is not None else [],
             "client": ("127.0.0.1", 5000), "server": ("127.0.0.1", 80)}
    sent = []
    received = False

    async def receive():
        nonlocal received
        if received:
            return {"type": "http.disconnect"}
        received = True
        return {"type": "http.request", "body": raw, "more_body": False}

    async def send(message):
        sent.append(message)

    await app(scope, receive, send)
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    data = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return status, data


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(webapp, "CHANNELS_FILE", str(tmp_path / "c.json"))
    monkeypatch.setattr(webapp, "_CHANNELS", None)
    monkeypatch.setenv("CHANNEL_ID", "-100999")
    return tmp_path


def test_channel_validation():
    assert valid_channel("@nome_ok") and valid_channel("@abcde")
    assert valid_channel("-100123") and valid_channel("-5")
    assert not valid_channel("ciao") and not valid_channel("@abc")
    assert not valid_channel("http://x") and not valid_channel("")
    assert not valid_channel(None)


def test_channels_crud(store):
    s = ChannelsStore(filepath=str(store / "c.json"))
    assert s.items[0]["id"] == "-100999"
    s.add("@secondo", "Secondo")
    assert len(s.items) == 2
    assert s.add("@secondo")["label"] == "Secondo"
    assert s.rename("@secondo", "Rino") is True
    assert s.rename("@fantasma", "X") is False
    assert s.remove("@secondo") is True
    assert s.remove("@fantasma") is False
    assert s.remove("-100999") is False


@pytest.mark.asyncio
async def test_gate_404_outside_prefix():
    for path in ("/", "/nope", "/s/wrong/", "/api/state"):
        status, _ = await _asgi(webapp.app, "GET", path)
        assert status == 404


@pytest.mark.asyncio
async def test_channels_http_crud(store):
    status, data = await _asgi(webapp.app, "GET", webapp.PREFIX + "/api/channels")
    assert status == 200 and len(json.loads(data)["channels"]) == 1
    status, data = await _asgi(webapp.app, "POST", webapp.PREFIX + "/api/channels",
                               {"id": "ciao"})
    assert status == 400
    status, data = await _asgi(webapp.app, "POST", webapp.PREFIX + "/api/channels",
                               {"id": "@nuovo"})
    assert status == 200 and json.loads(data)["created"] is True
    status, _ = await _asgi(webapp.app, "PUT", webapp.PREFIX + "/api/channels/@nuovo",
                            {"label": "Nuovo"})
    assert status == 200
    status, _ = await _asgi(webapp.app, "DELETE",
                            webapp.PREFIX + "/api/channels/@nuovo")
    assert status == 200
    status, _ = await _asgi(webapp.app, "DELETE",
                            webapp.PREFIX + "/api/channels/-100999")
    assert status == 409


@pytest.mark.asyncio
async def test_enqueue_forwards_target_and_titles(store, monkeypatch):
    seen = {}

    def fake_local(method, path, body=None, timeout=30):
        seen["payload"] = body
        return 200, {"ok": True, "position": 3}

    monkeypatch.setattr(webapp, "_local", fake_local)
    status, data = await _asgi(webapp.app, "POST", webapp.PREFIX + "/api/enqueue",
                               {"urls": ["https://example.com/v"], "quality": "2",
                                "channel": "-1001",
                                "titles": {"https://example.com/v": "Titolo"}})
    assert status == 200
    assert json.loads(data)["results"] == [{"url": "https://example.com/v", "position": 3}]
    assert seen["payload"]["target_channel"] == "-1001"
    assert seen["payload"]["quality"] == "720"
    assert seen["payload"]["title"] == "Titolo"


@pytest.mark.asyncio
async def test_enqueue_rejects_bad_input(store):
    status, _ = await _asgi(webapp.app, "POST", webapp.PREFIX + "/api/enqueue",
                            {"urls": [], "quality": "720"})
    assert status == 400
    status, _ = await _asgi(webapp.app, "POST", webapp.PREFIX + "/api/enqueue",
                            {"urls": ["https://example.com/v"], "quality": "8k"})
    assert status == 400
    status, _ = await _asgi(webapp.app, "POST", webapp.PREFIX + "/api/enqueue",
                            {"urls": ["https://example.com/v"], "quality": "720",
                             "channel": "nope"})
    assert status == 400


@pytest.mark.asyncio
async def test_state_proxies_local_api(store, monkeypatch):
    def fake_local(method, path, body=None, timeout=30):
        if path == "/api/queue":
            return 200, {"current": None, "items": [{"url": "u"}]}
        if path == "/api/progress":
            return 200, {"current": None, "jobs": {}}
        if path.startswith("/api/history"):
            return 200, {"ok": True, "entries": []}
        raise AssertionError(path)

    monkeypatch.setattr(webapp, "_local", fake_local)
    status, data = await _asgi(webapp.app, "GET", webapp.PREFIX + "/api/state")
    body = json.loads(data)
    assert status == 200 and body["bot_online"] is True
    assert len(body["items"]) == 1 and body["channels"]


@pytest.mark.asyncio
async def test_manifest_and_icon():
    status, data = await _asgi(webapp.app, "GET", webapp.PREFIX + "/manifest.json")
    assert status == 200
    body = json.loads(data)
    assert body["display"] == "standalone" and body["icons"]
    status, data = await _asgi(webapp.app, "GET", webapp.PREFIX + "/icon.svg")
    assert status == 200 and data.lstrip().startswith(b"<svg")


@pytest.mark.asyncio
async def test_state_bot_offline(store, monkeypatch):
    def _down(method, path, body=None, timeout=30):
        raise RuntimeError("bot offline: conn refused")

    monkeypatch.setattr(webapp, "_local", _down)
    status, data = await _asgi(webapp.app, "GET", webapp.PREFIX + "/api/state")
    assert status == 502 and json.loads(data)["error"] == "bot offline"
