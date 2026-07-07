# Design: Coda download multipli

Data: 2026-07-07
Status: Approvato (da implementare)

## Obiettivo

Permettere all'utente di mandare più link mentre un download è in corso.
I nuovi link vengono accodati automaticamente e processati in ordine FIFO.
La coda è persistente su disco: se il bot si riavvia, gli item in attesa
vengono mostrati all'utente che decide se riprendere o meno.

## Requisiti

1. Coda FIFO persistente in `data/queue.json`
2. Invio di un link mentre un download è in corso → aggiunto in fondo alla coda
3. Quando un download (download+upload) finisce, parte automaticamente il
   prossimo in coda
4. All'avvio del bot, se la coda non è vuota, mandare all'owner un messaggio
   con la lista degli item e chiedere conferma "procedere? (s/n)"
5. Comandi di gestione coda:
   - `/queue` — mostra download in corso + lista item in attesa (con posizione)
   - `/now <url>` — sposta l'item con quell'URL in cima alla coda (NON
     interrompe il download in corso; diventa il prossimo)
   - `/clean` — svuota tutta la coda (con conferma s/n)

## Concorrenza

Un solo worker. Un solo download alla volta. Il worker processa gli item
sequenzialmente. Non si scaricano/uploadano più video in parallelo
(motivi: stabilità connessione Telegram, limiti FloodWait, risorse disco).

## Componenti

### Nuovo: `bot/queue.py` — classe `DownloadQueue`

Gestione coda persistente. Schema file `data/queue.json`:

```json
[
  {
    "url": "https://...",
    "quality": "720",
    "title": "Titolo video",
    "ts": 1783435096.0
  }
]
```

API pubblica:

| Metodo | Descrizione |
|---|---|
| `__init__(filepath="data/queue.json")` | Carica da disco (o crea vuoto) |
| `add(url, quality, title)` | Aggiunge in fondo, salva su disco |
| `peek() -> item \| None` | Restituisce il primo SENZA rimuoverlo (fondamentale: vedi Persistenza) |
| `remove(url) -> bool` | Rimuove l'item con quell'URL (chiamato solo a download+upload COMPLETATO). Salva. |
| `move_front(url) -> bool` | Sposta l'item con quell'URL in testa. Ritorna False se non trovato. Salva. |
| `clear() -> int` | Svuota, ritorna numero di item rimossi. Salva. |
| `items -> list[dict]` | Lista in memoria, sincronizzata col file |
| `is_empty() -> bool` | True se la coda è vuota |

**Niente `pop()`.** Il worker usa `peek()` e `remove()`-solo-su-successo, così
un crash a metà download NON perde l'item (resta in cima alla coda e viene
riprocessato al riavvio).

In memoria tiene `self._items: list[dict]`; ogni modifica riscrive il file
con scrittura atomica (tempfile + os.replace, come fa `DownloadHistory`).

### Modifica: `bot/handlers.py`

**Stato globale:**

- `_is_downloading` → rimosso. Sostituito da `_queue: DownloadQueue` e
  `_current_item: dict | None` (item in lavorazione dal worker).
- `_cancel_requested` → resta (usato da `/stop` per interrompere il download
  corrente; dopo l'interruzione il worker passa al prossimo in coda).
- Nota: `/clean` NON interrompe il download in corso (che è già stato
  estratto dalla coda con `pop`). Svuota solo gli item in attesa. Per fermare
  quello in corso si usa `/stop`.

**Worker loop:**

```python
_queue_worker_task: asyncio.Task | None = None

async def _queue_worker(client, channel_id, owner_id):
    """Processa la coda: peek -> scarica+uploada -> remove (solo se successo).

    Usa peek()+remove(), NON pop(): un crash a metà download lascia l'item
    in cima alla coda, così al riavvio viene riprocessato (yt-dlp riprende
    i .part parziali automaticamente).
    """
    global _current_item
    while True:
        item = _queue.peek()
        if item is None:
            _current_item = None
            await asyncio.sleep(2)  # coda vuota, polling leggero
            continue
        _current_item = item
        # crea status_msg, chiama download_and_upload(...)
        # download_and_upload ritorna l'esito (ok/fallito/cancellato)
        # SU SUCCESSO: _queue.remove(item["url"])  -> item esce dalla coda
        # SU ERRORE:   _queue.remove(item["url"])  -> item esce (retry interni già fatti)
        # SU CANCEL:   _queue.remove(item["url"])  -> item esce, cleanup file
        # In tutti i casi l'item è rimosso solo quando il worker lo gestisce fino in fondo.
        # Se il bot CRASHA prima della fine: remove() non viene chiamata ->
        #   l'item resta in coda -> al riavvio viene riprocessato.
```

**Modifica a `_process_link`:**

Quando l'utente manda un link e sceglie la qualità, invece di chiamare
subito `download_and_upload`:

```python
if _current_item is not None or not _queue.is_empty():
    # c'è già qualcosa in corso → accoda
    _queue.add(url, quality, title)
    pos = len(_queue.items)
    await _safe_reply(event, f"📥 Aggiunto alla coda (posizione {pos}).")
else:
    # coda vuota e nessun download in corso → parte subito
    _queue.add(url, quality, title)
    # il worker lo prenderà al prossimo ciclo
```

Il worker è l'unico che chiama `download_and_upload`.

**Avvio (in `run.py` o all'avvio di handlers):**

**IMPORTANTE — cleanup condizionale (non distruttivo all'avvio):**

Prima il codice chiamava `cleanup_orphan_files()` incondizionatamente all'avvio
(`run.py:71`). Con la coda questo è DISTRUTTIVO: cancellerebbe i `.part` parziali
di un download interrotto dal crash, impedendo a yt-dlp di riprenderli. Ora:

1. Crea `DownloadQueue`
2. Se `_queue.is_empty()` è True:
   - Esegui `cleanup_orphan_files()` (file orfani veri, nessun item da riprendere)
   - Parte il worker subito
3. Se la coda NON è vuota:
   - **NON eseguire `cleanup_orphan_files()`** (i .part servono per il resume)
   - Manda all'owner un messaggio con la lista degli item in coda
   - Attende risposta s/n (usa meccanismo `_pending` con tipo `"confirm_resume"`)
   - Se "s" (riprendi) → il worker parte; yt-dlp riprenderà i `.part` parziali
   - Se "n" (non riprendere) → SOLO ORA esegui `cleanup_orphan_files()` +
     `_queue.clear()`; poi il worker parte (vuoto, pronto per link futuri)

Così l'utente può: mettere in coda → spegnere → riaccendere → "s" →
riprendere esattamente da dove era rimasto.

**Comandi:**

| Comando | Implementazione |
|---|---|
| `/queue` | Mostra: "🎬 In corso: `<title>` (`<quality>`)" + "📋 In coda (N):" + lista numerata url+title+quality |
| `/now <url>` | `_queue.move_front(url)` → "✅ Spostato in cima: `<title>`" o "❌ URL non in coda" |
| `/clean` | Invia "Vuoi svuotare la coda (N item)? s/n" → usa `_pending` tipo `"confirm_clean"` → su "s" chiama `_queue.clear()` |

### Modifica: `run.py`

- Passa `owner_id` ai comandi che ne hanno bisogno (già fatto)
- All'avvio, dopo `register_handlers`, attiva il worker (fare il controllo
  coda iniziale e poi `_queue_worker` come task di background)

## Cosa NON cambia

- Logica di download/upload in `download_and_upload` (già testata)
- Menu qualità e playlist (interazione utente → accodamento)
- `/stop` (interrompe il download corrente; il worker poi continua col prossimo)
- File history separato dalla coda
- Comandi admin esistenti (`/adduser`, `/users`, `/channel`, `/status`)

## Gestione errori e persistenza

### Riprendere dopo crash/riavvio (caso FREQUENTE — gestito con cura)

Scenario: utente mette in coda 3 link, il bot scarica il 1°, a metà
upload si spegne/crasha. Riavvio.

- Il 1° item (in lavorazione) **resta in coda** perché il worker usa
  `peek()`+`remove()`-su-successo, non `pop()`. Il `remove()` non è stato
  chiamato (crash prima della fine) → item ancora in `queue.json`.
- I `.part` parziali restano in `downloads/` (cleanup non eseguito all'avvio).
- All'avvio: prompt s/n. Su "s" → worker riprende il 1° item → yt-dlp
  riprende i `.part` da dove si era interrotto → upload → `remove()` →
  passa al 2° e 3°.

### Altri casi cleanup (verificati — lasciare invariati)

- `download_and_upload` cleanup su errore/cancel: OK — azione esplicita su
  un item in lavorazione, stato noto.
- Delete file dopo upload riuscito (`handlers.py:397`): OK — upload finito,
  file non più necessario.
- `/stop` quando idle (`handlers.py:793`): OK.
- `download_video` retry-esaurito (`downloader.py:286`): OK.
- `history.clear_orphan_entries` (`run.py:79`): OK — indipendente dalla coda.

### Se un download fallisce

L'item viene rimosso dalla coda (`remove(url)`) e registrato nella history come
errore. Il worker passa al prossimo. NON si ritenta lo stesso item in loop (il
retry interno di `download_video` copre già i tentativi di rete).

### Se `/stop` interrompe il download corrente

L'item viene rimosso dalla coda (`remove(url)`), cleanup file orfani, il worker
passa al prossimo in coda.

## Test

- `bot/queue.py`: test unitari su add/peek/remove/move_front/clear/persistenza
  (file temporaneo, ricarica dopo modifica, verifica che peek NON rimuova)
- `tests/test_queue.py` nuovo
- Test esistenti (`test_handlers`, `test_downloader`, `test_flow`) devono
  continuare a passare (la logica di download non cambia)

## Limiti / Out of scope

- Download paralleli: NO (per stabilità). Un worker, un download alla volta.
- Playlist automatica (scarica tutti i video di una playlist): fuori scope
  per ora, design futuro.
- Priorità per posizione (`/now 3`): fuori scope, solo per URL.
