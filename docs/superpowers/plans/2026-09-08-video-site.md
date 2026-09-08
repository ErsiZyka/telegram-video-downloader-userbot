# Video Site Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** sito LAN che replica il bot Telegram (download/coda/canali/cronologia/upload) parlando solo con la Local API del bot.

**Architecture:** `webapp.py` (FastAPI, secret-prefix, static `site.html` + JSON) → `requests` → Local API `bot/api.py` (localhost, single-writer di coda/history) → worker esistente esteso con `target_channel` e `kind="upload"`.

**Tech Stack:** Python 3.14, FastAPI/uvicorn/requests (già nel venv), stdlib resto, pytest.

**Spec:** `docs/superpowers/specs/2026-09-08-video-site-design.md`

## Global Constraints

- Il sito non apre mai la sessione Telethon e non scrive mai `queue.json`/`history.json`.
- Ogni estrazione metadati passa dal lock del bot (`_extract_lock`).
- Fuori da `/s/<token>/` → 404. Niente funzioni admin nel sito.
- `target_channel` assente = canale default. `kind` ignoti = errore in history, mai crash.
- Suite intera verde prima di ogni commit.

---

### Task 1: Local API — extras, move, clear, history-recent

**Files:**

- Modify: `bot/api.py` (`_handle_queue_add`, `do_GET`, `do_POST`)
- Test: `tests/test_api.py` (append)

**Interfaces:**

- Consumes: `h._get_queue() -> DownloadQueue` (ha `add(url, quality, title, headers, **extra)`, `move_front(url) -> bool`, `clear() -> int`, `remove(url)`, `items`), `h._get_history()._data: dict[str, dict]` con entry `{status, title, ts, ...}`.
- Produces: `POST /api/queue` accetta anche `kind/filepath/msg_id/page_url/target_channel`; `POST /api/move {url} -> {ok, moved}`; `POST /api/clear {} -> {ok, removed}`; `GET /api/history/recent?limit=20 -> {ok, entries: [{url, ...}]}` ordinate per `ts` desc.

- [ ] **Step 1: test extras passthrough + move/clear/recent**

```python
def test_queue_add_passthrough_extras(api):
    status, body = _call(api, "POST", "/api/queue",
                         {"url": "https://example.com/x", "quality": "720",
                          "target_channel": "-1001", "kind": "web"})
    assert status == 200
    _, q = _call(api, "GET", "/api/queue")
    assert q["items"][0]["target_channel"] == "-1001"


def test_move_and_clear(api):
    _call(api, "POST", "/api/queue", {"url": "https://example.com/a"})
    _call(api, "POST", "/api/queue", {"url": "https://example.com/b"})
    status, body = _call(api, "POST", "/api/move", {"url": "https://example.com/b"})
    assert (status, body["moved"]) == (200, True)
    _, q = _call(api, "GET", "/api/queue")
    assert q["items"][0]["url"] == "https://example.com/b"
    status, body = _call(api, "POST", "/api/clear", {})
    assert (status, body["removed"]) == (2,)
```

- [ ] **Step 2: run → FAIL** (`pytest tests/test_api.py -v`, extras ignorati, 404 su move/clear)
- [ ] **Step 3: implementa** — in `_handle_queue_add` raccogli `extra = {k: body[k] for k in ("kind","filepath","msg_id","page_url","target_channel") if k in body}` e `queue.add(url, quality, title, headers, **extra)`; aggiungi route `move`/`clear`/`history/recent` (recent: `sorted(hist._data.items(), key=lambda kv: kv[1].get("ts",0), reverse=True)[:limit]`, ogni entry con `"url"` dentro).
- [ ] **Step 4: run → PASS** (`pytest tests/test_api.py -q`)
- [ ] **Step 5: commit** (`git add bot/api.py tests/test_api.py`, `feat(api): extras, move, clear, history-recent`)

### Task 2: Local API — /api/analyze con loop bridge

**Files:**

- Modify: `bot/api.py` (nuovo `_handle_analyze`, `create_server(host, port, token, loop=None)`), `run.py` (passa il loop)
- Test: `tests/test_api.py` (analyze con extract_info mockato)

**Interfaces:**

- Consumes: `h.extract_info(url) -> dict | list` (sync, bloccante), `h._extract_lock: asyncio.Lock`, `get_extractor(url)`, `get_universal_fallback` NO (solo single-level: extractor dedicato → yt-dlp; il fallback resta al worker).
- Produces: `POST /api/analyze {url} -> {ok:true, kind:"single", title, duration, thumbnail, filesize_approx, direct_url, headers, fixed_quality} | {ok:true, kind:"playlist", videos:[{title, webpage_url}], url} | {ok:false, error}`. `server.bot_loop` settato da `run.py`; se `None`, usa esecutore dedicato senza lock (modalità test).

- [ ] **Step 1: test analyze**

```python
def test_analyze_single_and_playlist(api, monkeypatch):
    import bot.api as api_mod
    monkeypatch.setattr(api_mod.h, "extract_info",
                        lambda url: {"title": "T", "duration": 60, "thumbnail": "http://t/x.jpg",
                                     "webpage_url": url, "uploader": "u", "filesize_approx": 10})
    status, body = _call(api, "POST", "/api/analyze", {"url": "https://example.com/v"})
    assert (status, body["kind"], body["title"]) == (200, "single", "T")
    assert body["direct_url"] == "https://example.com/v"
```

- [ ] **Step 2: run → FAIL** (404 unknown endpoint)
- [ ] **Step 3: implementa** — `_handle_analyze`: valida http(s); se `get_extractor(url)` → `await` via bridge sotto `_extract_lock` → single con `direct_url=finfo.url, headers, fixed_quality`; altrimenti `extract_info` in thread (`loop.run_in_executor` via bridge, o diretto se `bot_loop is None`) sotto lock; lista → playlist; `ExtractError/VideoUnavailableError` → `{ok:false, error}`. Bridge: `asyncio.run_coroutine_threadsafe(coro, loop).result(timeout=120)`; in test (`bot_loop=None`) esegui tutto inline senza lock.
- [ ] **Step 4: run → PASS**
- [ ] **Step 5: commit** (`feat(api): analyze endpoint for site previews`)

### Task 3: Worker — target_channel + kind=upload

**Files:**

- Modify: `bot/handlers.py` (`_queue_worker`)
- Test: `tests/test_flow.py` o nuovo `tests/test_worker.py` (mock client, niente rete)

**Interfaces:**

- Consumes: item `{url, quality, title, headers?, kind?, filepath?, target_channel?}`.
- Produces: upload verso `target_channel` se presente; `kind="upload"` → `_upload_existing` senza download; `kind` ignoto → `history.set_error` + remove.

- [ ] **Step 1: test worker routing**

```python
@pytest.mark.asyncio
async def test_worker_uses_target_channel(monkeypatch):
    calls = {}
    async def fake_dl(client, status_msg, url, quality, title, channel_id, owner_id, headers=None):
        calls["channel"] = channel_id
        return "ok"
    monkeypatch.setattr(h, "download_and_upload", fake_dl)
    h._queue = DownloadQueue(filepath=str(tmp_path / "q.json"))
    h._queue.add("https://example.com/v", "720", "T", {}, target_channel="-1009")
    ...avvia _queue_worker con client finto, attendi svuotamento, cancella task...
    assert calls["channel"] == "-1009"
```

(dettagli del loop di attesa nel task; `kind="upload"` analogo con `_upload_existing` mockato e file reale in tmp; `kind="bogus"` → history error.)

- [ ] **Step 2: run → FAIL**
- [ ] **Step 3: implementa** — nel worker: `target = item.get("target_channel") or channel_id`; branch su `kind`: `"upload"` → check `filepath` esiste → `_upload_existing(client, status_msg, filepath, title, target, url)`; `"saved"` invariato; `"web"`/assente → `download_and_upload(..., target, ...)`; altro → `history.set_error(url, "kind non supportato", title)`.
- [ ] **Step 4: run → PASS** (tutta la suite)
- [ ] **Step 5: commit** (`feat(worker): target_channel + upload kind`)

### Task 4: Webapp core — auth, static, state, canali

**Files:**

- Create: `webapp.py`, `templates/site.html` (stile mockup approvato), `tests/test_webapp.py`
- Test: endpoint con `TestClient`? NO (httpx assente) → avvia `uvicorn`-free: usa `fastapi.testclient`? richiede httpx. Quindi: testa le funzioni pure (`_require_prefix`, validazione canale, load/save channels con tmp) + un server live su porta effimera via `urllib` come `test_api.py`.

**Interfaces:**

- Consumes: Local API via `requests` (`_local(method, path, body)` con bearer da `data/.local_api_token` o `LOCAL_API_TOKEN`); `data/channels.json` (solo webapp).
- Produces: middleware 404 fuori prefisso; `GET /` html; `GET /api/state`; canali CRUD con validazione `^(@[A-Za-z0-9_]{5,}|-100\d+|-\d+)$`; seed da `CHANNEL_ID`.

- [ ] **Step 1: test validazione + channels su tmp**

```python
def test_channel_validation():
    assert valid_channel("@nome_ok") and valid_channel("-100123")
    assert not valid_channel("ciao") and not valid_channel("http://x")


def test_channels_crud_tmp(tmp_path, monkeypatch):
    ...store puntato a tmp, add/list/rename/delete, default mai cancellabile se unico...
```

- [ ] **Step 2: run → FAIL**
- [ ] **Step 3: implementa** `webapp.py` + `ChannelsStore` (atomic write come `DownloadQueue`).
- [ ] **Step 4: run → PASS**
- [ ] **Step 5: commit** (`feat(site): webapp core + channels`)

### Task 5: Webapp — preview/enqueue/upload/queue-ops + UI

**Files:**

- Modify: `webapp.py`, `templates/site.html`
- Test: `tests/test_webapp.py` (enqueue chiama Local API mockata via `monkeypatch` su `_local`)

**Interfaces:**

- `POST /api/enqueue {urls[], quality, channel}` → per url: `POST local /api/queue {url, quality, title:"", headers:{}, target_channel}` (titolo vuoto: il worker usa `item.title or "Video"`? NO — adatta: se titolo vuoto il worker mostra l'URL. Decisione: la UI chiama prima `/api/preview` e invia il titolo ottenuto; se preview fallisce, titolo = url). Ritorna `[{url, position|error}]`.
- `POST /api/preview {url}` → proxy Local analyze.
- `POST /api/upload` (multipart `file`, `channel`) → salva `downloads/upload_<ts>_<safe>` (max 2GB via `is_over_limit` su content-length/stream), enqueue `kind="upload"`.
- `POST /api/priority|clear|cancel` → proxy Local.

- [ ] **Step 1: test enqueue con `_local` mockato** (asserisce payload `target_channel` e titolo da preview)
- [ ] **Step 2: run → FAIL**
- [ ] **Step 3: implementa** + UI `site.html`/JS (polling 2s su `/api/state`, azioni per riga)
- [ ] **Step 4: run → PASS** + reload manuale via curl contro server live di prova
- [ ] **Step 5: commit** (`feat(site): enqueue/preview/upload/ui`)

### Task 6: Deploy, docs, chiusura

- [ ] `.env.example` (+`SITE_TOKEN/PORT`), README sezione sito, `videosite.service`
- [ ] Suite verde, commit, restart `videobot`, `enable --now videosite`, curl live, un download vero di prova
- [ ] Report finale con link `http://192.168.0.27:8090/s/<token>/`
