# Design: Sistema Extractor Modulare (Playwright) per siti streaming

Data: 2026-07-07
Status: In attesa di approvazione

## Obiettivo

Scaricare video da siti streaming italiani (altadefinizione, streamingcommunity)
protetti da Cloudflare / JS, che yt-dlp non supporta. Sistema modulare: aggiungere
un nuovo sito = una sottoclasse di 2 righe.

## Contesto tecnico (verificato)

- yt-dlp NON ha extractor per vidxgo.co (player di altadefinizione) né per
  streamingcommunity. Il generic extractor fallisce.
- vidxgo.co ritorna **403 Cloudflare** a curl e yt-dlp → scraping HTTP impossibile.
- Playwright 1.61 + Chromium sono **già installati** sul sistema → nessuna
  dipendenza da scaricare.
- F12 è bloccato da streamingcommunity (JS) ma Playwright (browser headless)
  bypassa tutto: carica la pagina come un browser vero, Cloudflare passa,
  il player carica lo stream, intercettiamo l'm3u8 dal traffico di rete.

## Architettura

### File nuovi

```
bot/extractors/
├── __init__.py              ← registro: get_extractor(url) -> BaseExtractor | None
├── base.py                  ← VideoInfo, BaseExtractor (ABC), PlaywrightVideoExtractor
├── altadefinizione.py       ← AltaDefinizioneExtractor (sottoclasse, 2 righe)
└── streamingcommunity.py    ← StreamingCommunityExtractor (sottoclasse, 2 righe)
```

### Interfaccia

```python
# base.py
@dataclass
class VideoInfo:
    url: str        # URL diretto del video (m3u8/mp4) scaricabile da yt-dlp
    title: str

class BaseExtractor(ABC):
    DOMAINS: tuple[str, ...] = ()
    @classmethod
    def can_handle(cls, url: str) -> bool: ...  # match dominio
    @abstractmethod
    async def extract(self, url: str) -> VideoInfo: ...

class PlaywrightVideoExtractor(BaseExtractor):
    """Generic: lancia Chromium headless, naviga alla pagina, intercetta il
    primo m3u8/mp4 dal traffico di rete. Funziona per QUALSIASI sito
    Cloudflare-protetto il cui player carica uno stream HLS/MP4."""
    DOMAINS = ()
    async def extract(self, url) -> VideoInfo:
        # 1. launch chromium headless
        # 2. new context con User-Agent browser realistico
        # 3. page.on("response") -> cattura URL che contiene ".m3u8" o ".mp4"
        # 4. page.goto(url) + aspetta che video_url sia popolato (timeout 30s)
        # 5. title = await page.title()
        # 6. chiudi browser, ritorna VideoInfo
```

### Sottoclassi (un sito = 2 righe)

```python
# altadefinizione.py
class AltaDefinizioneExtractor(PlaywrightVideoExtractor):
    DOMAINS = ("altadefinizione.",)   # matcha .hot, .live, .gratis, etc.

# streamingcommunity.py
class StreamingCommunityExtractor(PlaywrightVideoExtractor):
    DOMAINS = ("streamingcommunity",) # matcha .pizza, .art, .vip, etc.
```

Aggiungere un sito futuro = creare un file con una classe da 2 righe.

### Integrazione in handlers.py

In `_process_link`, **prima** del flusso yt-dlp normale:

```python
extractor = get_extractor(url)
if extractor is not None:
    status_msg = await _safe_reply(event, "🌐 Estrazione video via browser...")
    try:
        info = await extractor.extract(url)   # async, Playwright async API
        title = info.title
        video_url = info.url
        await _safe_delete(status_msg)
        await _send_quality_menu(event, user_id, video_url, title)  # url = m3u8
        return
    except Exception as e:
        await _safe_edit(status_msg, f"❌ Estrazione fallita: {e}")
        return

# flusso yt-dlp normale (youtube, youporn, etc.) — invariato
...
```

### Cosa NON cambia

- `download_video` (yt-dlp scarica m3u8/mp4 generici perfettamente) — invariato
- Quality menu, progress, upload, caption — invariati
- Coda, history, comandi — invariati
- Extractor per siti yt-dlp (youtube, youporn) — invariati (yt-dlp gestisce)

### Flusso finale

```
Link streamingcommunity → get_extractor matcha → Playwright intercetta m3u8
  → menu qualità → enqueue m3u8 → yt-dlp scarica m3u8 → upload MP4 + caption

Link youtube → nessun match → yt-dlp normale (come prima)
```

## Comportamento runtime

- **Avvio estrazione**: Playwright lancia Chromium headless (~2-5s)
- **Timeout**: 30s per intercettare lo stream. Se scade → "Estrazione fallita"
- **Headless**: nessuna finestra visibile, gira in background
- **Per-video**: un browser nuovo per ogni estrazione (pulito, no stato condiviso)

## Limiti noti

- L'm3u8 è per-sessione: scade. Se l'utente reinvia il link del sito dopo ore,
  l'estrattore rifà l'estrazione (nuovo m3u8). La history "già scaricato"
  non scatta per questi siti (m3u8 diverso ogni volta) — accettabile.
- Qualità: yt-dlp su un m3u8 master trova le varianti (240p/480p/720p/1080p);
  il menu 360/720/1080/max filtra per height — funziona.
- Streamingcommunity potrebbe richiedere "click sul play" — se l'intercettazione
  non parte da sola, aggiungo un `page.click` sul pulsante play nell'extractor.

## Test

- `tests/test_extractors.py`: test `can_handle` per ogni extractor (match/no-match),
  mock di Playwright per `extract` (non lancia browser nei test).
- Test esistenti (76) devono continuare a passare.

## Dipendenze

- Playwright 1.61 + Chromium: **già installati** ✅
- requirements.txt: aggiungere `playwright>=1.40`
