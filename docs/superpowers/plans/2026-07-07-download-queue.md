# Coda Download Multipli — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Permettere download multipli accodati: mandi più link mentre uno scarica, vanno in coda FIFO persistente, e il worker li processa in sequenza. Comandi `/queue`, `/now <url>`, `/clean`. All'avvio, se la coda non è vuota, chiede conferma (s/n) e solo su "no" cancella i file parziali.

**Architecture:** Un nuovo `DownloadQueue` (persistente su `data/queue.json`) + un worker asincrono singolo che processa gli item in sequenza con `peek()`+`remove()`-su-successo (crash-safe: un item in lavorazione non viene perso se il bot si spegne). `download_and_upload` viene rifattorizzato per restituire un esito stringa e non usare più il guard `_is_downloading` (la serializzazione la fa il worker). L'avvio gestisce il prompt di resume e rende condizionale il cleanup.

**Tech Stack:** Python 3.12, Telethon 1.44, asyncio, JSON persistente, pytest/pytest-asyncio. Python 3.12 path: `C:/Users/ersi/AppData/Local/Programs/Python/Python312/python.exe`.

**Spec:** `docs/superpowers/specs/2026-07-07-download-queue-design.md`

## Global Constraints

- Un solo worker, un download alla volta (mai paralleli)
- Worker usa `peek()` + `remove()` (NON `pop()`): item rimosso solo a download/upload completato, così un crash non perde l'item
- Cleanup all'avvio (`run.py`) è CONDIZIONALE: se la coda non è vuota, NON cancellare i `.part` finché l'utente non dice "no"
- File history (`data/download_history.json`) resta separato dalla coda
- Tutti i 67 test esistenti devono continuare a passare
- Comandi owner-only (tranne `/queue` accessibile a chi può mandare link)
- Logica di download/upload in `download_and_upload` NON cambia (solo firma + esito)

## File Structure

- **Create:** `bot/queue.py` — classe `DownloadQueue` (coda persistente)
- **Create:** `tests/test_queue.py` — test unitari coda
- **Modify:** `bot/handlers.py` — globals (drop `_is_downloading`, add `_queue`/`_current_item`), worker loop, refactor `download_and_upload` (esito + drop `event`/guard), `_handle_selection` (enqueue + `confirm_resume`/`confirm_clean`), comandi `/queue` `/now` `/clean`, `cmd_status`/`cmd_stop` (usa `_current_item`), `start_queue_worker`
- **Modify:** `run.py` — cleanup condizionale, avvio worker, passa `owner_id` dove serve
- **Modify:** `tests/test_flow.py` — fixture `reset_state` (drop `_is_downloading`, reset `_queue`/`_current_item`)
- **Modify:** `bot/__init__.py` — nessun cambiamento richiesto (verifica)

---

## Task 1: DownloadQueue class + unit tests

**Files:**
- Create: `bot/queue.py`
- Test: `tests/test_queue.py`

**Interfaces:**
- Produces: `DownloadQueue(filepath)` con metodi `add(url,quality,title)->int`, `peek()->dict|None`, `remove(url)->bool`, `move_front(url)->bool`, `clear()->int`, property `items`, `is_empty()->bool`

- [ ] **Step 1: Write failing tests**

Create `tests/test_queue.py`:

```python
import json
import os
import tempfile
import pytest
from bot.queue import DownloadQueue


@pytest.fixture
def tmp_queue():
    d = tempfile.mkdtemp()
    fp = os.path.join(d, "queue.json")
    yield DownloadQueue(filepath=fp), fp
    import shutil
    shutil.rmtree(d, ignore_errors=True)


def test_empty_queue_is_empty(tmp_queue):
    q, _ = tmp_queue
    assert q.is_empty() is True
    assert q.peek() is None
    assert q.items == []


def test_add_returns_position(tmp_queue):
    q, _ = tmp_queue
    pos1 = q.add("https://a", "720", "A")
    assert pos1 == 1
    pos2 = q.add("https://b", "max", "B")
    assert pos2 == 2
    assert q.is_empty() is False
    assert len(q.items) == 2


def test_peek_does_not_remove(tmp_queue):
    q, _ = tmp_queue
    q.add("https://a", "720", "A")
    q.add("https://b", "max", "B")
    first = q.peek()
    assert first["url"] == "https://a"
    assert len(q.items) == 2  # NOT removed
    assert q.peek()["url"] == "https://a"  # still there


def test_remove_by_url(tmp_queue):
    q, _ = tmp_queue
    q.add("https://a", "720", "A")
    q.add("https://b", "max", "B")
    assert q.remove("https://a") is True
    assert q.peek()["url"] == "https://b"
    assert q.remove("https://nonexistent") is False


def test_move_front(tmp_queue):
    q, _ = tmp_queue
    q.add("https://a", "720", "A")
    q.add("https://b", "max", "B")
    q.add("https://c", "360", "C")
    assert q.move_front("https://c") is True
    assert q.items[0]["url"] == "https://c"
    assert q.items[1]["url"] == "https://a"
    assert q.move_front("https://nonexistent") is False
    # move_front on already-first is no-op but True
    assert q.move_front("https://c") is True
    assert q.items[0]["url"] == "https://c"


def test_clear(tmp_queue):
    q, _ = tmp_queue
    q.add("https://a", "720", "A")
    q.add("https://b", "max", "B")
    n = q.clear()
    assert n == 2
    assert q.is_empty() is True


def test_persistence(tmp_queue):
    q, fp = tmp_queue
    q.add("https://a", "720", "A")
    q.add("https://b", "max", "B")
    q.move_front("https://b")
    # reload from same file
    q2 = DownloadQueue(filepath=fp)
    assert [i["url"] for i in q2.items] == ["https://b", "https://a"]


def test_corrupt_file_treated_as_empty(tmp_queue):
    q, fp = tmp_queue
    with open(fp, "w") as f:
        f.write("NOT JSON{{{")
    q2 = DownloadQueue(filepath=fp)
    assert q2.is_empty() is True


def test_non_list_json_treated_as_empty(tmp_queue):
    q, fp = tmp_queue
    with open(fp, "w") as f:
        json.dump({"not": "a list"}, f)
    q2 = DownloadQueue(filepath=fp)
    assert q2.is_empty() is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `C:/Users/ersi/AppData/Local/Programs/Python/Python312/python.exe -m pytest tests/test_queue.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'bot.queue'`

- [ ] **Step 3: Implement DownloadQueue**

Create `bot/queue.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `C:/Users/ersi/AppData/Local/Programs/Python/Python312/python.exe -m pytest tests/test_queue.py -v`
Expected: PASS (9 tests)

- [ ] **Step 5: Run full suite to confirm nothing broke**

Run: `C:/Users/ersi/AppData/Local/Programs/Python/Python312/python.exe -m pytest tests/ -q`
Expected: PASS (76 tests = 67 existing + 9 new)

- [ ] **Step 6: Commit**

```bash
git add bot/queue.py tests/test_queue.py
git commit -m "feat: add persistent DownloadQueue (crash-safe peek/remove)"
```

---

## Task 2: Refactor download_and_upload — return outcome, drop guard

**Files:**
- Modify: `bot/handlers.py:209-417` (download_and_upload)
- Modify: `tests/test_flow.py:38-44` (reset_state fixture: drop _is_downloading)

**Interfaces:**
- Consumes: nothing new
- Produces: `download_and_upload(client, status_msg, url, quality, title, channel_id, owner_id, max_retries=3) -> str` returning one of `"ok"`, `"error"`, `"cancelled"`. No longer takes `event`; no longer guards on `_is_downloading`.

**Why:** The worker (Task 3) will be the only caller. The worker serializes downloads, so the `_is_downloading` guard is obsolete. The worker needs to know the outcome to decide whether to remove the item from the queue (it removes on every outcome; only a crash leaves the item).

- [ ] **Step 1: Update reset_state fixture in test_flow.py**

In `tests/test_flow.py`, the `reset_state` fixture sets `h._is_downloading`. Since Task 2 removes `_is_downloading`, replace those lines. Edit the fixture (around lines 38-44):

Replace:
```python
@pytest.fixture(autouse=True)
def reset_state():
    h._pending.clear()
    h._is_downloading = False
    h._cancel_requested = False
    h._edit_muted_until = 0.0
    yield
    h._pending.clear()
    h._is_downloading = False
```
With:
```python
@pytest.fixture(autouse=True)
def reset_state():
    h._pending.clear()
    h._cancel_requested = False
    h._edit_muted_until = 0.0
    h._current_item = None
    if h._queue is not None:
        h._queue.clear()
    yield
    h._pending.clear()
    h._current_item = None
    if h._queue is not None:
        h._queue.clear()
```

Note: `h._queue` and `h._current_item` are added in Task 3. To keep this task independent, instead do a defensive version that tolerates either state. Use this form instead:

```python
@pytest.fixture(autouse=True)
def reset_state():
    h._pending.clear()
    h._cancel_requested = False
    h._edit_muted_until = 0.0
    for attr in ("_current_item",):
        if hasattr(h, attr):
            setattr(h, attr, None)
    if hasattr(h, "_queue") and h._queue is not None:
        h._queue.clear()
    yield
    h._pending.clear()
    for attr in ("_current_item",):
        if hasattr(h, attr):
            setattr(h, attr, None)
    if hasattr(h, "_queue") and h._queue is not None:
        h._queue.clear()
```

Keep the existing `h._is_downloading = False` lines REMOVED. (Removing them is safe because Task 2 deletes the global; leaving them would raise AttributeError.)

- [ ] **Step 2: Refactor download_and_upload signature and guard**

In `bot/handlers.py`, replace the function header + guard (lines 209-220):

```python
async def download_and_upload(
    client, event, status_msg, url: str, quality: str, title: str, channel_id: int, max_retries: int = 3,
) -> None:
    global _is_downloading, _cancel_requested

    if _is_downloading:
        await _safe_reply(event, "⏳ C'è già un download in corso.")
        return
    _is_downloading = True
    _cancel_requested = False
    _log(f"Download iniziato: {url} [{quality}]")

    loop = asyncio.get_running_loop()

    try:
```
With:
```python
async def download_and_upload(
    client, status_msg, url: str, quality: str, title: str,
    channel_id: int, owner_id: int, max_retries: int = 3,
) -> str:
    """Download then upload a video. Returns outcome: 'ok' | 'error' | 'cancelled'.

    The caller (the queue worker) is responsible for serialization. Status
    updates are sent by editing `status_msg`; final errors go to `owner_id`.
    """
    global _cancel_requested
    _cancel_requested = False
    _log(f"Download iniziato: {url} [{quality}]")

    loop = asyncio.get_running_loop()

    try:
```

- [ ] **Step 3: Convert return statements to outcome strings**

Within `download_and_upload`, change every `return` (and the final fallthrough) to return an outcome string. Use these exact replacements (search for each unique snippet):

1. The two CancelDownload download-phase returns (around line 251 and 263):
```python
            except CancelDownload:
                cleanup_orphan_files()
                await _safe_edit(status_msg, "🛑 Download annullato.")
                return
```
→
```python
            except CancelDownload:
                cleanup_orphan_files()
                await _safe_edit(status_msg, "🛑 Download annullato.")
                return "cancelled"
```
(apply to BOTH CancelDownload download blocks — there are two identical blocks; edit each)

2. The DownloadError-exhausted block (around line 287):
```python
        if filepath is None:
            cleanup_orphan_files()
            await _safe_edit(status_msg,
                f"❌ Download fallito dopo {max_retries} tentativi.\nErrore: {download_error}")
            try:
                _get_history().set_error(url, download_error or "download fallito", title)
            except Exception:
                pass
            return
```
→
```python
        if filepath is None:
            cleanup_orphan_files()
            await _safe_edit(status_msg,
                f"❌ Download fallito dopo {max_retries} tentativi.\nErrore: {download_error}")
            try:
                _get_history().set_error(url, download_error or "download fallito", title)
            except Exception:
                pass
            return "error"
```

3. The too-large block (around line 302):
```python
        if file_size_gb > 2:
            if os.path.exists(filepath):
                os.remove(filepath)
            await _safe_edit(status_msg,
                f"❌ File troppo grande ({file_size_gb:.1f} GB). Limite Telegram: 2GB")
            return
```
→
```python
        if file_size_gb > 2:
            if os.path.exists(filepath):
                os.remove(filepath)
            await _safe_edit(status_msg,
                f"❌ File troppo grande ({file_size_gb:.1f} GB). Limite Telegram: 2GB")
            return "error"
```

4. The Exception-during-download block (around line 273-278):
```python
            except Exception as e:
                if _cancel_requested:
                    cleanup_orphan_files()
                    await _safe_edit(status_msg, "🛑 Download annullato.")
                    return
                download_error = repr(e)
                _log(f"Errore download (tentativo {attempt}): {download_error}")
                cleanup_orphan_files()
                await _safe_edit(status_msg, f"❌ Download fallito: {e}")
                try:
                    _get_history().set_error(url, str(e), title)
                except Exception:
                    pass
                return
```
→
```python
            except Exception as e:
                if _cancel_requested:
                    cleanup_orphan_files()
                    await _safe_edit(status_msg, "🛑 Download annullato.")
                    return "cancelled"
                download_error = repr(e)
                _log(f"Errore download (tentativo {attempt}): {download_error}")
                cleanup_orphan_files()
                await _safe_edit(status_msg, f"❌ Download fallito: {e}")
                try:
                    _get_history().set_error(url, str(e), title)
                except Exception:
                    pass
                return "error"
```

5. The upload-phase CancelDownload block (around line 373-379):
```python
            except CancelDownload:
                if os.path.exists(filepath):
                    try:
                        os.remove(filepath)
                    except Exception:
                        pass
                await _safe_edit(status_msg, "🛑 Upload annullato. File eliminato.")
                return
```
→
```python
            except CancelDownload:
                if os.path.exists(filepath):
                    try:
                        os.remove(filepath)
                    except Exception:
                        pass
                await _safe_edit(status_msg, "🛑 Upload annullato. File eliminato.")
                return "cancelled"
```

6. The upload cancel-during-exception blocks (around lines 385-391):
```python
            except Exception as e:
                if _cancel_requested:
                    if os.path.exists(filepath):
                        try:
                            os.remove(filepath)
                        except Exception:
                            pass
                    await _safe_edit(status_msg, "🛑 Upload annullato. File eliminato.")
                    return
```
→
```python
            except Exception as e:
                if _cancel_requested:
                    if os.path.exists(filepath):
                        try:
                            os.remove(filepath)
                        except Exception:
                            pass
                    await _safe_edit(status_msg, "🛑 Upload annullato. File eliminato.")
                    return "cancelled"
```

7. The success and final-error tail of download_and_upload (the block after the upload retry loop, around lines 397-417):
```python
        if upload_success:
            await _safe_edit(status_msg, "✅ Video inviato con successo al canale!")
            try:
                _get_history().set_success(url, filepath, title)
            except Exception as e:
                _log(f"history save failed: {e!r}")
        else:
            await _safe_edit(status_msg,
                f"❌ Upload fallito dopo {max_retries} tentativi.\nErrore: {upload_error}")
            try:
                _get_history().set_error(url, upload_error or "upload fallito", title)
            except Exception:
                pass

    finally:
        _is_downloading = False
```
→
```python
        if upload_success:
            await _safe_edit(status_msg, "✅ Video inviato con successo al canale!")
            try:
                _get_history().set_success(url, filepath, title)
            except Exception as e:
                _log(f"history save failed: {e!r}")
            return "ok"
        else:
            await _safe_edit(status_msg,
                f"❌ Upload fallito dopo {max_retries} tentativi.\nErrore: {upload_error}")
            try:
                _get_history().set_error(url, upload_error or "upload fallito", title)
            except Exception:
                pass
            return "error"

    finally:
        pass
```

(Leave `finally: pass` to preserve the try/except structure; the `_is_downloading` reset is gone.)

- [ ] **Step 4: Remove the `_is_downloading` global definition**

In `bot/handlers.py`, delete the line:
```python
_is_downloading = False
```
(around line 35). Keep `_cancel_requested = False`.

- [ ] **Step 5: Update the one caller in _handle_selection (temporary)**

The quality branch (around line 666) currently calls `download_and_upload(client, event, status_msg, ...)`. This task changes the signature, so update the call to match (the full enqueue wiring is Task 3). Replace:
```python
        url = pending["url"]
        title = pending["title"]
        _clear_pending(user_id)
        _log(f"Qualità scelta da {user_id}: {quality}")
        status_msg = await _safe_reply(event, f"⏳ Avvio download in qualità **{label}**...")
        await download_and_upload(client, event, status_msg, url, quality, title, channel_id)
        return
```
With (temporary direct call with new signature; Task 3 replaces with enqueue):
```python
        url = pending["url"]
        title = pending["title"]
        _clear_pending(user_id)
        _log(f"Qualità scelta da {user_id}: {quality}")
        status_msg = await _safe_reply(event, f"⏳ Avvio download in qualità **{label}**...")
        await download_and_upload(client, status_msg, url, quality, title, channel_id, event.sender_id)
        return
```

- [ ] **Step 6: Run full test suite**

Run: `C:/Users/ersi/AppData/Local/Programs/Python/Python312/python.exe -m pytest tests/ -q`
Expected: PASS (76 tests). If a test references `_is_downloading`, grep and fix:
```bash
grep -rn "_is_downloading" bot/ tests/
```

- [ ] **Step 7: Commit**

```bash
git add bot/handlers.py tests/test_flow.py
git commit -m "refactor: download_and_upload returns outcome string, drop _is_downloading guard"
```

---

## Task 3: Queue worker + enqueue integration

**Files:**
- Modify: `bot/handlers.py` — add globals `_queue`/`_current_item`/`_resume_*`, worker `_queue_worker`, `start_queue_worker`, modify `_handle_selection` quality branch to enqueue, modify `cmd_status`/`cmd_stop` to use `_current_item`, add `confirm_resume`/`confirm_clean` handling in `_handle_selection`

**Interfaces:**
- Consumes: `DownloadQueue` (Task 1), `download_and_upload` outcome (Task 2)
- Produces: `start_queue_worker(client, channel_id, owner_id)` coroutine to call from `run.py`

- [ ] **Step 1: Add queue globals and init helper**

Near the top of `bot/handlers.py` (after `_cancel_requested = False`, replacing the deleted `_is_downloading`), add:

```python
from bot.queue import DownloadQueue

# ─── State ───
_cancel_requested = False
_edit_muted_until = 0.0

_queue: DownloadQueue | None = None
_current_item: dict | None = None
_queue_worker_task: asyncio.Task | None = None
_resume_event: asyncio.Event | None = None
_resume_decision: str | None = None  # "yes" | "no"
```

Add an init function (after `_get_history`):

```python
def _get_queue() -> DownloadQueue:
    global _queue
    if _queue is None:
        _queue = DownloadQueue(filepath="data/queue.json")
    return _queue
```

- [ ] **Step 2: Add the worker loop**

Add after `download_and_upload` (before `_upload_existing`):

```python
async def _queue_worker(client, channel_id: int, owner_id: int) -> None:
    """Process the queue sequentially: peek -> download+upload -> remove.

    Uses peek()+remove() (NOT pop()): an item is removed only after the
    worker finishes handling it. If the bot crashes mid-download, the item
    remains in the queue and is reprocessed on next startup (yt-dlp resumes
    .part files). Removed regardless of outcome (ok/error/cancelled) — only
    a crash leaves it, by design.
    """
    global _current_item
    queue = _get_queue()
    _log("Queue worker avviato")
    while True:
        item = queue.peek()
        if item is None:
            _current_item = None
            await asyncio.sleep(2)  # coda vuota, polling leggero
            continue
        _current_item = item
        url = item["url"]
        quality = item.get("quality", "720")
        title = item.get("title", "Video")
        _log(f"Worker processa: {url} [{quality}]")
        try:
            status_msg = await client.send_message(
                owner_id, f"⏳ Avvio download: **{_escape_md(title)}** [{quality}]..."
            )
            await download_and_upload(
                client, status_msg, url, quality, title, channel_id, owner_id
            )
        except Exception as e:
            _log(f"Worker errore inatteso su {url}: {e!r}")
        finally:
            # Always remove: the item has been handled (ok/error/cancelled).
            # If we crashed before reaching here, the item stays — that's the
            # crash-safe property; on restart it gets reprocessed.
            queue.remove(url)
            _current_item = None
```

- [ ] **Step 3: Add start_queue_worker (startup resume prompt)**

Add after `_queue_worker`:

```python
async def start_queue_worker(client, channel_id: int, owner_id: int) -> None:
    """Called once at startup. Handles the resume prompt, then starts worker.

    If the queue has items from a previous run, asks the owner whether to
    resume. Only on "no" does it delete partial files and clear the queue
    (the conditional cleanup — destructive cleanup is deferred to user choice).
    On "yes" (or empty queue) the worker starts; yt-dlp resumes .part files.
    """
    global _resume_event, _resume_decision, _queue_worker_task
    queue = _get_queue()
    if queue.is_empty():
        # Truly orphan files (no queue items) -> safe to clean now.
        cleanup_orphan_files()
    else:
        lines = ["📥 Ci sono download in coda dal precedente avvio:\n"]
        for i, it in enumerate(queue.items, 1):
            lines.append(f"{i}. {_escape_md((it.get('title') or 'Sconosciuto')[:55])} [{it.get('quality','?')}]")
        lines.append("\nScrivi `si` per riprendere (i file parziali verranno continuati) "
                     "o `no` per annullare e pulire.")
        await client.send_message(owner_id, "\n".join(lines))
        _resume_event = asyncio.Event()
        _set_pending(owner_id, {"type": "confirm_resume"})
        await _resume_event.wait()
        decision = _resume_decision
        _resume_event = None
        _resume_decision = None
        if decision == "no":
            cleanup_orphan_files()
            queue.clear()
            await client.send_message(owner_id, "📭 Coda svuotata e file parziali rimossi.")
        else:
            await client.send_message(owner_id, "▶️ Riprendo la coda...")
    _queue_worker_task = asyncio.create_task(_queue_worker(client, channel_id, owner_id))
```

- [ ] **Step 4: Replace _handle_selection quality branch with enqueue**

Replace the temporary direct call from Task 2 (the quality branch):

```python
        url = pending["url"]
        title = pending["title"]
        _clear_pending(user_id)
        _log(f"Qualità scelta da {user_id}: {quality}")
        status_msg = await _safe_reply(event, f"⏳ Avvio download in qualità **{label}**...")
        await download_and_upload(client, status_msg, url, quality, title, channel_id, event.sender_id)
        return
```
With:
```python
        url = pending["url"]
        title = pending["title"]
        _clear_pending(user_id)
        _log(f"Qualità scelta da {user_id}: {quality}")
        queue = _get_queue()
        pos = queue.add(url, quality, title)
        if _current_item is None and pos == 1:
            await _safe_reply(event, f"⏳ Avvio download in qualità **{label}**...")
        else:
            await _safe_reply(event,
                f"📥 Aggiunto alla coda (posizione {pos}). "
                f"Verrà scaricato al termine di quello in corso.")
        return
```

- [ ] **Step 5: Add confirm_resume and confirm_clean handling in _handle_selection**

At the top of `_handle_selection`, right after `text_lower = text.lower().strip()`, add (before the `confirm_retry` block):

```python
    if pending["type"] == "confirm_resume":
        global _resume_decision
        _clear_pending(user_id)
        _resume_decision = "yes" if text_lower in ("si", "sì", "yes", "y") else "no"
        if _resume_event is not None:
            _resume_event.set()
        return

    if pending["type"] == "confirm_clean":
        _clear_pending(user_id)
        if text_lower in ("si", "sì", "yes", "y"):
            queue = _get_queue()
            n = queue.clear()
            await _safe_reply(event, f"🧹 Coda svuotata ({n} item rimossi).")
        else:
            await _safe_reply(event, "👌 Operazione annullata.")
        return
```

- [ ] **Step 6: Update cmd_status and cmd_stop to use _current_item**

Replace `cmd_status`:
```python
async def cmd_status(client, event):
    await _safe_reply(event, f"📊 Download in corso: {'sì' if _is_downloading else 'no'}")
```
With:
```python
async def cmd_status(client, event):
    queue = _get_queue()
    pending = len(queue.items)
    if _current_item is not None:
        line = f"📊 In corso: **{_escape_md((_current_item.get('title') or '')[:55])}**"
    else:
        line = "📊 Nessun download in corso."
    line += f"\n📋 In coda: {pending}"
    await _safe_reply(event, line)
```

Replace `cmd_stop`'s guard:
```python
    if _is_downloading:
```
With:
```python
    if _current_item is not None:
```

- [ ] **Step 7: Run full test suite**

Run: `C:/Users/ersi/AppData/Local/Programs/Python/Python312/python.exe -m pytest tests/ -q`
Expected: PASS (76 tests). The flow tests mock extract_info and check pending state; the quality branch now enqueues instead of downloading, so `test_link_sets_quality_pending` still passes (it checks pending type == "quality" before the number is sent).

If any test fails because it asserted a direct `download_video` call, inspect and adjust — but per the existing tests, none call into the quality→download path.

- [ ] **Step 8: Commit**

```bash
git add bot/handlers.py
git commit -m "feat: queue worker, enqueue on quality choice, resume prompt, status/stop use _current_item"
```

---

## Task 4: Commands /queue, /now, /clean

**Files:**
- Modify: `bot/handlers.py` — add `cmd_queue`, `cmd_now`, `cmd_clean`; register them in `register_handlers`

**Interfaces:**
- Consumes: `_get_queue()`, `_set_pending`
- Produces: three new owner commands

- [ ] **Step 1: Add the three command functions**

Add near the other `cmd_*` functions (before `cmd_status`):

```python
async def cmd_queue(client, event, owner_id: int) -> None:
    if event.sender_id != owner_id:
        return
    queue = _get_queue()
    lines = []
    if _current_item is not None:
        lines.append(f"🎬 In corso: **{_escape_md((_current_item.get('title') or '')[:55])}** [{_current_item.get('quality','?')}]")
    else:
        lines.append("🎬 Nessun download in corso.")
    items = queue.items
    lines.append(f"\n📋 In coda ({len(items)}):")
    if not items:
        lines.append("_(vuota)_")
    else:
        for i, it in enumerate(items, 1):
            lines.append(f"{i}. {_escape_md((it.get('title') or 'Sconosciuto')[:55])} [{it.get('quality','?')}]")
    await _safe_reply(event, "\n".join(lines))


async def cmd_now(client, event, owner_id: int) -> None:
    if event.sender_id != owner_id:
        return
    parts = (event.message.text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await _safe_reply(event, "❌ Uso: `/now <url>`")
        return
    url = parts[1].strip()
    queue = _get_queue()
    if queue.move_front(url):
        await _safe_reply(event, f"✅ Spostato in cima alla coda: `{url}`")
    else:
        await _safe_reply(event, f"❌ URL non presente in coda: `{url}`")


async def cmd_clean(client, event, owner_id: int) -> None:
    if event.sender_id != owner_id:
        return
    queue = _get_queue()
    if queue.is_empty() and _current_item is None:
        await _safe_reply(event, "📭 La coda è già vuota.")
        return
    n = len(queue.items)
    await _safe_reply(event, f"🧹 Vuoi svuotare la coda ({n} item in attesa)? Scrivi `si` o `no`.")
    _set_pending(event.sender_id, {"type": "confirm_clean"})
```

- [ ] **Step 2: Register the commands in register_handlers**

In `register_handlers`'s `_on_message`, add the new command branches (owner block). After the `/stop` branch, before the final `await on_message(...)`:

```python
            elif text.startswith("/queue"):
                await cmd_queue(client, event, owner_id)
                return
            elif text.startswith("/now"):
                await cmd_now(client, event, owner_id)
                return
            elif text.startswith("/clean"):
                await cmd_clean(client, event, owner_id)
                return
```

- [ ] **Step 3: Run full test suite**

Run: `C:/Users/ersi/AppData/Local/Programs/Python/Python312/python.exe -m pytest tests/ -q`
Expected: PASS (76 tests)

- [ ] **Step 4: Commit**

```bash
git add bot/handlers.py
git commit -m "feat: add /queue, /now, /clean commands"
```

---

## Task 5: Startup wiring in run.py + conditional cleanup

**Files:**
- Modify: `run.py` — make startup cleanup conditional, start the queue worker after peer cache

**Interfaces:**
- Consumes: `start_queue_worker` (Task 3)

- [ ] **Step 1: Move the orphan cleanup out of run.py (now handled by start_queue_worker)**

In `run.py`, the startup currently does (around lines 71-79):
```python
    removed = cleanup_orphan_files(download_dir)
    if removed:
        print(f"Puliti {len(removed)} file orfani")

    history = DownloadHistory(filepath="data/download_history.json")
    removed_resolved = history.purge_resolved_errors()
    if removed_resolved:
        print(f"Pulite {removed_resolved} entry di errore già risolte dalla cronologia")
    removed_h = history.clear_orphan_entries(download_dir)
    if removed_h:
        print(f"Pulite {removed_h} entry orfane dalla cronologia")
```

Replace with (remove the unconditional `cleanup_orphan_files`; keep history):
```python
    history = DownloadHistory(filepath="data/download_history.json")
    removed_resolved = history.purge_resolved_errors()
    if removed_resolved:
        print(f"Pulite {removed_resolved} entry di errore già risolte dalla cronologia")
    removed_h = history.clear_orphan_entries(download_dir)
    if removed_h:
        print(f"Pulite {removed_h} entry orfane dalla cronologia")
```

Note: `cleanup_orphan_files(download_dir)` is now called inside `start_queue_worker` (conditional). Remove the import of `cleanup_orphan_files` from `run.py` if it becomes unused:
```python
from bot.downloader import check_dependencies
```
(keep `check_dependencies`; drop `cleanup_orphan_files` from the import since run.py no longer calls it directly).

- [ ] **Step 2: Start the queue worker after peer cache population**

In `run.py`, after the `client.iter_dialogs()` peer cache block and the channel resolution block (and before `await client.run_until_disconnected()`), add:

```python
    # Start the download queue worker (handles resume prompt if queue non-empty)
    from bot.handlers import start_queue_worker
    await start_queue_worker(client, config["channel_id"], config["owner_id"])
```

Place it right before `await client.run_until_disconnected()`.

- [ ] **Step 3: Verify run.py imports are consistent**

Run:
```bash
grep -n "cleanup_orphan_files" run.py
```
Expected: no output (the only remaining uses are inside handlers.py and downloader.py).

- [ ] **Step 4: Run full test suite**

Run: `C:/Users/ersi/AppData/Local/Programs/Python/Python312/python.exe -m pytest tests/ -q`
Expected: PASS (76 tests)

- [ ] **Step 5: Smoke test — bot boots**

Run (background, 25s):
```bash
cd /c/Users/ersi/video_downloader_bot
rm -f /tmp/bot.log
PYTHONIOENCODING=utf-8 C:/Users/ersi/AppData/Local/Programs/Python/Python312/python.exe -u run.py > /tmp/bot.log 2>&1 &
sleep 22
cat /tmp/bot.log
kill %1 2>/dev/null
```
Expected log contains "Dipendenze OK", "Userbot avviato e in ascolto", "Canale risolto", and (since the queue should be empty) NO resume prompt. If `data/queue.json` is empty `{}`/`[]`, the worker starts silently.

- [ ] **Step 6: Commit**

```bash
git add run.py
git commit -m "feat: conditional startup cleanup + queue worker boot in run.py"
```

---

## Task 6: Regression + edge-case verification

**Files:**
- Verify: all tests pass, bot boots, queue file created

- [ ] **Step 1: Full test suite**

Run: `C:/Users/ersi/AppData/Local/Programs/Python/Python312/python.exe -m pytest tests/ -q`
Expected: PASS (76 tests)

- [ ] **Step 2: Verify queue file created on first run**

Run:
```bash
ls -la /c/Users/ersi/video_downloader_bot/data/queue.json
cat /c/Users/ersi/video_downloader_bot/data/queue.json
```
Expected: file exists, content `[]` (empty list).

- [ ] **Step 3: Verify no leftover _is_downloading references**

Run:
```bash
grep -rn "_is_downloading" /c/Users/ersi/video_downloader_bot/bot /c/Users/ersi/video_downloader_bot/tests /c/Users/ersi/video_downloader_bot/run.py
```
Expected: no output.

- [ ] **Step 4: Final commit if any cleanup**

```bash
git status
```
If clean, done. If stray changes, commit them with a clear message.

---

## Self-Review Checklist (completed by implementer)

- [ ] Spec coverage: queue persistence (Task 1), worker peek/remove crash-safe (Task 2+3), resume prompt s/n (Task 3+5), conditional cleanup (Task 5), `/queue` `/now` `/clean` (Task 4) — all present
- [ ] No `pop()` used anywhere (only peek+remove)
- [ ] Startup cleanup is conditional (only on "no" or empty queue)
- [ ] All 76 tests pass
- [ ] Bot boots and worker starts with empty queue
