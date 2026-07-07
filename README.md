# Telegram Video Downloader Userbot

Userbot Telegram (Telethon) che scarica video da qualsiasi link supportato da yt-dlp (YouTube, TikTok, Instagram, Twitter/X, Vimeo, YouPorn e centinaia di altri) **e** da siti streaming italiani protetti da Cloudflare (altadefinizione, streamingcommunity) tramite un sistema di estrazione modulare basato su Playwright. I video vengono postati automaticamente in un canale Telegram come **MP4 riproducibile** (non file generico) con didascalia contenente il link originale.

Una **coda di download persistente** (FIFO, crash-safe) gestisce più richieste in sequenza, e un **sistema di logging centralizzato** mostra ogni passo in tempo reale sia a console che su file.

---

## Cosa è cambiato rispetto alla prima versione

La prima versione era un semplice wrapper Pyrogram + yt-dlp. Questa versione è una riscrittura completa con Telethon, coda, estrattori, logging e molti fix. Ecco tutte le differenze:

### Architettura
| Prima (v1) | Ora (v2) |
|---|---|
| **Pyrogram** | **Telethon** + FastTelethon (upload chunk paralleli) |
| Upload seriale (1 connessione MTProto, ~2-5 MB/s) | **FastTelethon** con upload parallelo (più veloce, più stabile su file grandi) |
| I bottoni inline NON funzionavano con userbot | Ancora non funzionano (limite Telegram per userbot) → interazione via testo |
| Nessuna coda — un solo download alla volta, gli altri persi | **Coda FIFO persistente** (`data/queue.json`), crash-safe, resume automatico |
| Nessun logging strutturato (`print()` sparsi) | **Logging centralizzato** con timestamp, file rotante `data/bot.log` + console, cattura anche yt-dlp/Telethon/Playwright |
| yt-dlp soltanto — siti Cloudflare (altadefinizione, streamingcommunity) → 403/404 | **Sistema estrattori modulare** (Playwright headless) per siti Cloudflare-protetti |
| Nessuna cronologia persistente | `data/download_history.json` con dedup, retry, purge errori risolti |

### Funzionalità nuove
- **Coda di download** (`bot/queue.py`): FIFO persistente su `data/queue.json`. Peek+remove (non pop) → crash-safe: se il bot muore durante un download, l'item rimane e viene rielaborato al riavvio. Comandi `/queue`, `/now <url>`, `/clean`.
- **Estrattori streaming** (`bot/extractors/`): Playwright headless Chromium apre la pagina del film, intercetta l'URL m3u8/mp4 dal traffico di rete (anche dentro iframe cross-origin), clicca play via `evaluate()` per bypassare i controlli viewport, e restituisce URL + header anti-leech. Aggiungere un sito nuovo = 2 righe (subclass con `DOMAINS=(...)`).
- **Upload come video riproducibile**: `probe_video_metadata()` usa ffprobe per ottenere durata/dimensioni reali → `DocumentAttributeVideo` → Telegram mostra il file come video con streaming + thumbnail, non come documento generico.
- **Didascalia con link originale**: ogni upload ha una didascalia Markdown con link inline al video sorgente.
- **Pulizia URL automatica**: `extract_url()` rimuove virgolette/virgole/parentesi finali che si incollano per errore.
- **Messaggio utile per link player diretti**: vidxgo/vidplay diretti → messaggio chiaro invece di 404 incomprensibile.
- **Resume FloodWait-aware**: gli edit del messaggio di progresso vengono mutati durante FloodWait (non genera altri floodwait), ma il progresso continua a loggarsi nel file ogni 5s.
- **Pulizia startup condizionale**: non cancella i `.part` all'avvio se la coda ha item (per non perdere download parziali riprendibili).
- **Fallback formato**: se yt-dlp dice "Requested format is not available", riprova automaticamente con `best`.
- **Fix YouPorn `it.` subdomain**: `it.youporn.com` scaricava SVG invece del video → `_normalize_url()` riscrive in `www.youporn.com` prima di yt-dlp.

### Fix di bug
- `confirm_retry` loop infinito (history → retry → history...)
- Nome file reale per `InputFile`/`InputFileBig` in `fasttelethon.py`
- Qualità "360" su vixcloud (non esiste 360p, minimo 480p) → fallback `height<=480`
- `asyncio event loop` e `DocumentAttributeVideo` (record stale già risolti)

### Numeri
- **87 test** passano (76 originali + 11 estrattori)
- **6 moduli** bot: `client`, `handlers`, `downloader`, `history`, `queue`, `logging_config`, `whitelist`, `fasttelethon`
- **3 estrattori**: base (Playwright), altadefinizione, streamingcommunity

---

## Come funziona

1. Hai un numero di telefono **secondario** che fa da userbot (Telethon)
2. Dal tuo numero **principale** (whitelistato) mandi un link in chat privata all'userbot
3. L'userbot controlla se il link corrisponde a un **estrattore** (altadefinizione, streamingcommunity):
   - Sì → Playwright apre la pagina, estrae l'URL m3u8 + header anti-leech
   - No → yt-dlp `extract_info` diretto
4. Ti mostra un menu per scegliere la qualità (**360p, 720p, 1080p, MAX**) — rispondi scrivendo `1`/`2`/`3`/`4` (i bottoni inline NON funzionano con userbot)
5. Se è una playlist, ti mostra la lista dei video con titoli — scegli quali
6. Il download entra in **coda** (FIFO). Se un download è in corso, aspetta il suo turno
7. L'userbot scarica (yt-dlp, con header anti-leech per i siti streaming) e posta nel canale come **video MP4 riproducibile** con didascalia
8. Il file viene cancellato dal PC dopo l'upload
9. Ogni passo è **loggato** con timestamp nel file `data/bot.log` e a console

## Requisiti

- **Python 3.10+** (sviluppato su 3.12)
- **ffmpeg** + **ffprobe** (per merge audio/video e probe metadati)
- **Playwright** + Chromium (per gli estrattori — `playwright install chromium` al primo setup)
- Un account Telegram secondario (per l'userbot)
- L'userbot deve essere **admin** del canale di destinazione

## Installazione

```bash
# 1. Clona
git clone https://github.com/ErsiZyka/telegram-video-downloader-userbot.git
cd telegram-video-downloader-userbot

# 2. Installa le dipendenze Python
pip install -r requirements.txt

# 3. Installa Chromium per Playwright (gli estrattori)
playwright install chromium

# 4. Configura
cp .env.example .env
# Modifica .env con i tuoi dati (vedi sotto)
```

## Configurazione (.env)

```bash
# 1. Vai su https://my.telegram.org, fai login col numero secondario
#    Crea una app in "API development tools" e ottieni:
API_ID=1234567
API_HASH=abc123def456...

# 2. Canale di destinazione (dove i video vengono postati)
#    L'userbot deve essere admin del canale.
#    Per ottenere l'ID: inoltra un messaggio dal canale a @RawDataBot
CHANNEL_ID=-1001234567890

# 3. Il tuo ID Telegram (numero principale)
#    Ottienilo da @RawDataBot o @userinfobot
OWNER_ID=123456789

# 4. Opzionali
DOWNLOAD_DIR=downloads/
MAX_RETRIES=3
MAX_FILE_SIZE_GB=2
PROGRESS_UPDATE_INTERVAL=2

# Cookie YouTube (RISOLVE "Sign in to confirm you're not a bot")
# Imposta il browser dove sei loggato su YouTube:
#   chrome | edge | firefox | brave | chromium | opera | vivaldi | whale
COOKIES_FROM_BROWSER=chrome
```

## Avvio

### Modo semplice (Windows)
Doppio click su **`start.bat`** → avvia il bot in una finestra.

### Con finestra log live (consigliato)
Doppio click su **`start_with_log.bat`** → apre **due** finestre:
- Bot (con log a console)
- **Log live** che taila `data/bot.log` aggiornandosi in tempo reale

### Manuale
```bash
python run.py
```

La prima volta Telethon ti chiederà:
1. Numero di telefono (es: `+393331234567`)
2. Codice di verifica che arriva su Telegram

Dopo il login, viene creato il file `my_video_downloader_bot.session`. I successivi avvii partono istantaneamente.

## Comandi

Solo tu (OWNER_ID) puoi usare questi comandi in chat privata con l'userbot. **Scrivi come testo** (i bottoni inline non funzionano con userbot).

| Comando | Descrizione |
|---------|-------------|
| `/adduser @username` | Aggiunge un utente alla whitelist |
| `/adduser 123456789` | Aggiunge un utente per ID |
| `/removeuser @username` | Rimuove dalla whitelist |
| `/users` | Elenca tutti gli utenti autorizzati |
| `/channel` | Mostra il canale di destinazione |
| `/status` | Download in corso + coda |
| `/stop` | Ferma download/upload corrente, pulisce file |
| `/queue` | Mostra la coda di download |
| `/now <url>` | Sposta un URL in cima alla coda (priorità) |
| `/clean` | Svuota la coda (con conferma) |

**Selezione qualità**: dopo aver mandato un link, rispondi con `1` (360p), `2` (720p), `3` (1080p), `4` (MAX).

## Siti supportati

### yt-dlp nativo
YouTube, TikTok, Instagram, Twitter/X, Vimeo, YouPorn, e [centinaia di altri](https://github.com/yt-dlp/yt-dlp/blob/master/supportedsites.md).

### Estrattori Playwright (siti streaming italiani)
| Sito | Dominio | Come usare |
|---|---|---|
| AltaDefinizione | `altadefinizione.*` | Manda il link della pagina del film |
| StreamingCommunity | `streamingcommunityz.pizza` | Manda il link della pagina del film/serie |

⚠️ **Importante**: manda il link della **pagina del film/serie**, NON il link del player interno (vidxgo/vidplay) — quegli URL funzionano solo incorporati e ritornano 403/404 se aperti direttamente.

### Aggiungere un nuovo sito
Crea un file `bot/extractors/tuosito.py` con 2 righe:
```python
from bot.extractors.base import PlaywrightVideoExtractor

class MioSitoExtractor(PlaywrightVideoExtractor):
    DOMAINS = ("miosito",)
```
Aggiungilo a `_EXTRACTORS` in `bot/extractors/__init__.py`. Fatto. Playwright gestisce il resto.

## Test

```bash
python -m pytest tests/ -v
# 87 test passano
```

## Struttura del progetto

```
telegram-video-downloader-userbot/
├── .env                    # Configurazione (non committare!)
├── .env.example            # Template configurazione
├── run.py                  # Entry point
├── requirements.txt
├── start.bat               # Avvio bot (Windows)
├── start_with_log.bat      # Avvio bot + finestra log live (Windows)
├── log.bat                 # Finestra log live (Windows)
├── bot/
│   ├── __init__.py
│   ├── client.py           # Telethon Client setup
│   ├── handlers.py         # Message/callback handlers, download+upload flow
│   ├── downloader.py       # yt-dlp wrapper, retry, progress, ffprobe
│   ├── fasttelethon.py     # Upload chunk paralleli (FastTelethon)
│   ├── history.py          # Cronologia persistente su JSON
│   ├── queue.py            # Coda FIFO persistente (crash-safe)
│   ├── logging_config.py   # Logging centralizzato (console + file)
│   ├── whitelist.py        # Whitelist persistente su JSON
│   └── extractors/         # Sistema estrattori modulare
│       ├── __init__.py     # Registry get_extractor(url)
│       ├── base.py         # PlaywrightVideoExtractor + VideoInfo + header anti-leech
│       ├── altadefinizione.py
│       └── streamingcommunity.py
├── data/
│   ├── bot.log             # Log persistente (rotante 2MB x3)
│   ├── queue.json          # Coda di download
│   ├── download_history.json
│   └── whitelist.json
├── downloads/              # Directory temporanea download
└── tests/
    ├── test_whitelist.py
    ├── test_downloader.py
    ├── test_handlers.py
    ├── test_flow.py
    ├── test_queue.py
    └── test_extractors.py
```

## Limitazioni note

### ⚠️ Velocità download dai siti streaming italiani
I CDN italiani (vixcloud per streamingcommunity, vidxgo per altadefinizione) **throttano a ~0.5 MB/s per IP** = circa la bitrate di playback (1.8 Mbps per 720p). Non è un limite del bot — è il server.

- **720p** (~500-900 MB) → 15-30 minuti
- **1080p / MAX** (~1-2 GB) → 30-60+ minuti
- Fragments concorrenti (4) non aiutano: vixcloud limita per IP, non per connessione
- aria2c (multi-connessione per fragment) non testato — probabilmente throttle comunque

**Consiglio**: usa **480p** per test rapidi, **720p** per qualità/velocità bilanciate.

### ⚠️ Velocità upload a Telegram
Telegram limita la banda per account **non-Premium** a ~2-5 MB/s. FastTelethon usa chunk paralleli ma il tetto MTProto resta.

**Per superare 5 MB/s**: Telegram Premium sull'account userbot.

### Bottoni inline
I bottoni inline (inline keyboard) **NON funzionano** con userbot (account utente, non bot). Tutta l'interazione è via **testo**: scrivi `1`/`2`/`3`/`4` per la qualità, `si`/`no` per le conferme.

### Limite file
- **2 GB** per file (limite Telegram). File più grandi vengono rifiutati.
- `MAX_FILE_SIZE_GB` in `.env` controlla il controllo pre-upload.

### YouTube
Richiede cookie browser (`COOKIES_FROM_BROWSER` in `.env`) per risolvere "Sign in to confirm you're not a bot". Imposta il browser dove sei loggato su YouTube.

### YouPorn
Il subdomain `it.youporn.com` viene normalizzato a `www.youporn.com` (altrimenti yt-dlp scarica un SVG invece del video).

### Link player diretti (vidxgo/vidplay)
I link del tipo `v.vidxgo.co/...` o `vidplay.site/...` **non funzionano** — sono URL interni del player che vivono solo dentro un iframe. Manda il link della **pagina del film** su altadefinizione/streamingcommunity e il bot estrae il player automaticamente.

### Estractor Playwright
- Avvia un Chromium headless ad ogni estrazione (~3-10 secondi di overhead)
- Richiede `playwright install chromium` al primo setup
- Se il sito cambia struttura DOM, l'estrattore può fallire (il click play cerca `.btn-play`/simili via JS)
- L'URL m3u8 ha un token con scadenza (~6 ore) — se il download resta in coda troppo a lungo, il token può scadere e serve ri-estrarre

### Coda
- Singolo worker, **un download alla volta** (per stabilità Telegram)
- Se il bot crasha durante un download, l'item rimane in coda e viene rielaborato al riavvio (yt-dlp riprende i `.part`)
- `/clean` svuota tutta la coda (con conferma `si`/`no`)

### yt-dlp
- Va aggiornato periodicamente: `pip install -U yt-dlp` (i siti cambiano spesso)
- Per siti non supportati nativamente serve un estrattore Playwright

### Altro
- L'userbot deve essere **admin** del canale di destinazione
- La sessione (`my_video_downloader_bot.session`) è legata al numero di telefono — non condividerla
- I log (`data/bot.log`) ruotano a 2MB con 3 backup

---

## Licenza

Uso personale. yt-dlp, Telethon, Playwright hanno le rispettive licenze.
