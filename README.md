# Telegram Video Downloader Userbot

Userbot Telegram (Pyrogram) che scarica video da qualsiasi link (YouTube, TikTok, Instagram, Twitter/X, Vimeo e centinaia di altri siti) e li posta automaticamente in un canale Telegram.

## Come funziona

1. Hai un numero di telefono **secondario** che fa da userbot
2. Dal tuo numero **principale** (whitelistato) mandi un link in chat privata all'userbot
3. L'userbot analizza il link e ti mostra un menu per scegliere la qualità (360p, 720p, 1080p, MAX)
4. Se è una playlist, ti mostra la lista dei video con titoli — scegli quali scaricare
5. L'userbot scarica il video e lo posta nel canale Telegram configurato
6. Il file viene cancellato dal PC dopo l'upload

## Requisiti

- Python 3.10+
- [yt-dlp](https://github.com/yt-dlp/yt-dlp)
- [ffmpeg](https://ffmpeg.org/download.html) (per il merge audio/video)
- Un account Telegram secondario (per l'userbot)

## Installazione

```bash
# 1. Clona o scarica i file
cd video_downloader_bot

# 2. Installa le dipendenze
pip install -r requirements.txt

# 3. Configura
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
```

## Primo avvio

```bash
python run.py
```

La prima volta Pyrogram ti chiederà:
1. Numero di telefono (es: `+393331234567`)
2. Codice di verifica che arriva su Telegram

Dopo il login, viene creato il file `my_video_downloader_bot.session`. I successivi avvii partono istantaneamente.

## Comandi Admin

Solo tu (OWNER_ID) puoi usare questi comandi in chat privata con l'userbot:

| Comando | Descrizione |
|---------|-------------|
| `/adduser @username` | Aggiunge un utente alla whitelist |
| `/adduser 123456789` | Aggiunge un utente per ID |
| `/removeuser @username` | Rimuove dalla whitelist |
| `/users` | Elenca tutti gli utenti autorizzati |
| `/channel` | Mostra il canale di destinazione |
| `/status` | Download attivi e in coda |

## Test

```bash
python -m pytest tests/ -v
```

## Struttura del progetto

```
video_downloader_bot/
├── .env                    # Configurazione (non committare!)
├── .env.example            # Template configurazione
├── run.py                  # Entry point
├── requirements.txt
├── bot/
│   ├── __init__.py
│   ├── client.py           # Pyrogram Client setup
│   ├── handlers.py         # Message/callback handlers, download+upload flow
│   ├── downloader.py       # yt-dlp wrapper, retry, progress
│   └── whitelist.py        # Whitelist persistente su JSON
├── data/
│   └── whitelist.json      # Utenti autorizzati
├── downloads/              # Directory temporanea download
└── tests/
    ├── test_whitelist.py
    ├── test_downloader.py
    ├── test_handlers.py
    └── test_flow.py
```

## Comandi disponibili

| Comando | Descrizione |
|---------|-------------|
| `/adduser @username` | Aggiunge un utente alla whitelist |
| `/removeuser @username` | Rimuove dalla whitelist |
| `/users` | Elenca gli utenti autorizzati |
| `/channel` | Mostra il canale di destinazione |
| `/status` | Download in corso? |
| `/stop` | Ferma download/upload e pulisce file |

## Limitazioni note

### Velocita upload
Telegram limita la banda per account non-Premium a circa 2-5 MB/s per connessione.
Pyrogram usa una singola connessione TCP MTProto, quindi questo e' il tetto massimo.

**Ottimizzazioni applicate:**
- tgcrypto (crittografia C) - obbligatorio per performance decenti
- workers=32 nel Client Pyrogram
- Progress bar ogni 5s + rispetto FloodWait

**Cose provate e smentite:**
- uvloop: non supporta Windows
- cryptogram: piu' lento di tgcrypto (205 vs 293 MB/s)
- pyro-tg-fork: non esiste su PyPI
- Upload parallelo: richiede Telethon + FastTelethon (riscrivere tutto)

**Per superare 5 MB/s:** Telegram Premium o riscrivere in Telethon + FastTelethon.

### Altri limiti
- File video: max 2 GB (limite Telegram)
- YouTube: richiede cookie browser (COOKIES_FROM_BROWSER in .env)
- Userbot deve essere admin del canale
- I bottoni inline NON funzionano con userbot (interazione via testo)
- yt-dlp va aggiornato: pip install -U yt-dlp
