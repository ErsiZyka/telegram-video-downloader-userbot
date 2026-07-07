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
| `pop() -> item \| None` | Rimuove e restituisce il primo (o None se vuota) |
| `peek() -> item \| None` | Restituisce il primo senza rimuoverlo |
| `move_front(url) -> bool` | Sposta l'item con quell'URL in testa. Ritorna False se non trovato. Salva. |
| `clear() -> int` | Svuota, ritorna numero di item rimossi. Salva. |
| `items -> list[dict]` | Lista in memoria, sincronizzata col file |
| `is_empty() -> bool` | True se la coda è vuota |

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
    """Processa la coda: prende un item, lo scarica+uploada, poi il prossimo."""
    global _current_item
    while True:
        item = _queue.pop()
        if item is None:
            _current_item = None
            await asyncio.sleep(2)  # coda vuota, polling leggero
            continue
        _current_item = item
        # crea status_msg, chiama download_and_upload(...)
        # al termine (successo o errore) il loop continua al prossimo item
```

Avviato una volta sola all'avvio del bot, come task in background.

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

1. Crea `DownloadQueue`
2. Se `_queue.is_empty()` è False:
   - Manda all'owner un messaggio con la lista degli item in coda
   - Attende risposta s/n (usa meccanismo `_pending` con tipo `"confirm_resume"`)
   - Se "n" → svuota la coda (`_queue.clear()`); il worker parte comunque
     (così i link futuri funzionano normalmente)
   - Se "s" → parte il worker
3. Se la coda è vuota → parte il worker subito

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

## Gestione errori

- Se un download fallisce per un item in coda, l'item viene rimosso (pop)
  e registrato nella history come errore. Il worker passa al prossimo.
  NON si ritenta lo stesso item in loop (il retry interno di `download_video`
  copre già i tentativi di rete).
- Se `/stop` interrompe il download corrente: l'item viene rimosso (cleanup
  file orfani), il worker passa al prossimo in coda.

## Test

- `bot/queue.py`: test unitari su add/pop/move_front/clear/persistenza
  (file temporaneo, ricarica dopo modifica)
- `tests/test_queue.py` nuovo
- Test esistenti (`test_handlers`, `test_downloader`, `test_flow`) devono
  continuare a passare (la logica di download non cambia)

## Limiti / Out of scope

- Download paralleli: NO (per stabilità). Un worker, un download alla volta.
- Playlist automatica (scarica tutti i video di una playlist): fuori scope
  per ora, design futuro.
- Priorità per posizione (`/now 3`): fuori scope, solo per URL.
