# Video Site — Design Spec (V1)

**Stato:** approvato dall'utente (design in chat + mockup `videosite-mockup/index.html`, tema scuro editoriale).
**Goal:** sito in LAN che replica 1:1 le funzioni del bot Telegram + canali multipli.
**Non-goal V1:** utenti nominali, accesso da internet, HTTPS pubblico, funzioni admin, split file > limite.

## 1. Accesso e sicurezza

- Unico link segreto: tutto vive sotto `/s/<SITE_TOKEN>/`, resto → 404 (non 403).
- Solo rete di casa: uvicorn su `0.0.0.0:8090`, niente port-forward; documentato.
- Niente funzioni admin nel sito (no shell/processi/servizi).
- `SITE_TOKEN` (32+ char) da env; se assente, generato e appeso a `.env` una volta.
- Enqueue accetta solo `http(s)://` e `tg://`; la API locale resta su `127.0.0.1:8091` con bearer.

## 2. Pagine (tutte sotto il prefisso segreto)

1. **Download** — N righe link (batch), selettore canale, qualità 360/720/1080/Max
   (nascosta se qualità fissa), card live con progresso, card si/no per già-scaricati/errori.
2. **Canali** — lista (default evidenziato), collega via ID/`@username` (check formato
   subito, nome vero al primo upload), rimuovi (mai il default se è l'unico).
3. **Coda** — stato live + lista (= /status /queue) con metti-primo, rimuovi, svuota, ferma
   (= /now /clean /stop).
4. **Cronologia** — ultimi ok/errori con riprova e dimentica (= si/no del bot).
5. **File** — upload dal dispositivo → canale scelto (= /save e media inoltrati).

## 3. Architettura (vincolo fisso)

- Il sito NON apre mai la sessione Telethon e NON scrive mai `queue.json`/`history.json`.
- Unico ponte: **Local API del bot** (`bot/api.py`, localhost). Il sito la chiama con `requests`
  (già nel venv) dentro endpoint FastAPI sincroni (= threadpool di uvicorn, niente loop).
- Estrazione metadati (titoli/anteprime) passa SEMPRE dal bot: nuovo `POST /api/analyze`
  che gira sotto il `_extract_lock` esistente (un'estrazione alla volta, anti rate-limit).
  Il sito non chiama mai yt-dlp/Playwright in proprio.

## 4. Contratti Local API (aggiunte a `bot/api.py`)

- `POST /api/analyze {url}` → `{ok, kind: "single"|"playlist"|"error", ...}`.
  Single: `{title, duration, thumbnail, filesize_approx, direct_url, headers, fixed_quality}`.
  Playlist: `{videos: [{title, webpage_url}], url}`. Error: `{error}`.
  Eseguito via `asyncio.run_coroutine_threadsafe` sul loop del bot (passato da `run.py`).
- `POST /api/queue` esteso: accetta extra `kind`, `filepath`, `msg_id`, `page_url`,
  `target_channel` (pass-through a `DownloadQueue.add`).
- `POST /api/move {url}` → `move_front`; `POST /api/clear {}` → `{removed}`.
- `GET /api/history/recent?limit=20` → entries ordinate per `ts` desc.

## 5. Worker (modifiche a `bot/handlers.py`)

- `_queue_worker`: `target = item.get("target_channel") or channel_id`; lo passa a
  `download_and_upload` e `download_and_upload_saved` al posto del fisso.
- `kind == "upload"`: salta il download, verifica `item["filepath"]`, chiama
  `_upload_existing(..., target, ...)` (= stesso lifecycle del reupload da /save).
- `kind` ignoti → errore in history, mai crash.

## 6. Webapp (`webapp.py` + `templates/site.html` + `static/`)

- FastAPI; middleware: path deve iniziare con `/s/{token}/` altrimenti 404.
- `GET /` → `site.html` (stesso stile del mockup approvato, fetch JSON, polling 2s).
- `GET /api/state` → `{queue, current, progress, channels, history}` (fan-out verso Local API).
- `POST /api/enqueue {urls[], quality, channel}` → per ogni url: dedupe come `on_message`,
  `POST` a Local `/api/queue`. Ritorna posizioni.
- `POST /api/preview {url}` → proxy di Local `/api/analyze` (per titoli/anteprime/batch).
- Canali `GET/POST/DELETE /api/channels`, `PUT /api/channels/{id}` (rename). File:
  `data/channels.json` (solo webapp). Seed: `CHANNEL_ID` env come default.
- `POST /api/upload` (multipart + `channel`) → salva `downloads/upload_<ts>_<safe>`,
  enqueue `kind="upload"` via Local API.
- `POST /api/priority {url}`, `POST /api/clear`, `POST /api/cancel {job?}` → proxy Local.

## 7. Dati ed env

- `data/channels.json`: `[{id, label, added_ts}]` — solo la webapp lo scrive.
- `.env`: `SITE_TOKEN`, `SITE_PORT=8090`, `LOCAL_API_*` già esistenti.
- Job in coda: `target_channel` opzionale (assente = canale default del bot).

## 8. Deploy e rollback

- `videosite.service` (systemd, After=videobot). Restart `videobot` per le nuove API/worker
  (coda crash-safe, resume `.part`). Rollback: `systemctl stop videosite` + restart videobot
  su `main` — il bot ignora campi extra ignoti? NO: worker su main non conosce
  `target_channel`/`upload` → per rollback svuotare la coda prima. Documentato.

## 9. Test

- Nuovi pytest: endpoint Local API aggiunti, worker target/upload-kind (mock client),
  validazione canali webapp, passthrough extra. Suite intera verde prima del deploy.
- Manuale: curl su Local API + sito, un download vero di prova nel canale.
