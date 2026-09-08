# Telegram Video Downloader Userbot

<p align="center">
  <img alt="Python" src="https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white">
  <img alt="Telethon" src="https://img.shields.io/badge/Telethon-latest-2CA5E0?logo=telegram&logoColor=white">
  <img alt="yt-dlp" src="https://img.shields.io/badge/yt--dlp-latest-red">
  <img alt="Playwright" src="https://img.shields.io/badge/Playwright-Chromium-2EAD33">
  <img alt="License" src="https://img.shields.io/badge/License-MIT-green">
</p>

A self-hosted **Telegram userbot** that downloads videos from any link supported by
`yt-dlp` (YouTube, TikTok, Instagram, Twitter/X, Vimeo, and hundreds more) **and** from
Cloudflare-protected streaming sites — then automatically posts them to your Telegram
channel as a **playable MP4** with the original link in the caption.

Everything runs on your own machine (PC, server, or an old Android phone via Termux) using
a secondary Telegram account. Requests go through a **persistent, crash-safe FIFO queue** and
every step is logged to both console and a rotating log file.

---

## Table of contents

- [Features](#features)
- [How it works](#how-it-works)
- [Requirements](#requirements)
- [Quick start](#quick-start)
  - [1. Install dependencies](#1-install-dependencies)
  - [2. Configure](#2-configure)
  - [3. Start the bot](#3-start-the-bot)
- [Admin commands](#admin-commands)
- [How extraction works (3-level architecture)](#how-extraction-works-3-level-architecture)
- [Supported sites](#supported-sites)
- [Advanced configuration](#advanced-configuration)
- [Content policy](#content-policy)
- [Deploy](#deploy)
  - [systemd (Linux server)](#systemd-linux-server)
  - [screen](#screen)
  - [Android / Termux](#android--termux)
- [Troubleshooting](#troubleshooting)
- [Testing](#testing)
- [Project structure](#project-structure)
- [Contributing](#contributing)
- [License](#license)

---

## Features

- **Persistent FIFO download queue** (`data/queue.json`) that survives restarts. The worker
  uses `peek()` + `remove()` (not `pop()`), so if the bot crashes mid-download the item stays
  in the queue and is reprocessed on the next start (yt-dlp resumes the `.part` files).
- **Universal 3-level extraction** — dedicated extractors → `yt-dlp` → universal Playwright
  fallback that handles **dozens of video sites**.
- **Parallel upload via FastTelethon** — many TCP connections at once, up to 10–20 MB/s, sent
  as a **reproducible MP4** (real metadata probed with `ffprobe` so Telegram shows streaming +
  thumbnail) with a clickable caption linking back to the original video and site.
- **User whitelist** — only the owner and explicitly authorized users can use the bot.
- **Download history with dedup** — `data/download_history.json` remembers outcomes; re-sending
  a link offers a fast re-upload or retry instead of re-downloading blindly.
- **Admin commands** — manage the whitelist, inspect/download the queue, stop downloads, etc.
- **Choose quality per download** — `360p`, `720p`, `1080p`, or `MAX`.
- **Playlists** — a paginated numbered menu lets you pick which videos to download (reply with
  index, `next`, or `prev`).
- **Retry with exponential backoff** for both download and upload.
- **Slow-start detector** — if the first seconds of a download are throttled (single-digit
  KB/s), the bot restarts with more concurrent connections (up to 64).
- **TLS browser impersonation** (`curl_cffi`) to defeat Cloudflare/Akamai 403 blocks.
- **Browser-header fallback on 403** — blocked requests retry automatically with realistic
  browser `User-Agent`/`Accept` headers.

---

## How it works

1. You run the userbot on a **secondary** phone number (the "worker" account).
2. From your **main** number (or any whitelisted user), you send a video link in a private
   chat with the userbot.
3. The bot analyzes the link (specific extractor → `yt-dlp` → universal fallback).
4. It shows a **quality menu** — reply with `1`/`2`/`3`/`4` (inline buttons don't work with
   userbots, so interaction is text-based).
5. For playlists, you get a **numbered list** and choose the videos.
6. The video enters the FIFO queue and downloads — one at a time.
7. It is uploaded to the channel as a **playable MP4** with a caption containing the original
   link.
8. The local file is deleted after upload, and every step is logged to `data/bot.log`.

---

## Requirements

- **Python 3.10+** (tested on 3.12–3.14)
- **ffmpeg** and **ffprobe** (audio/video merge + metadata probing)
- **yt-dlp** (installed via `requirements.txt`)
- **Playwright + Chromium** (for streaming-site extractors — `playwright install chromium`)
- A **secondary Telegram account** for the userbot
- The userbot must be **admin** of the destination channel

---

## Quick start

### 1. Install dependencies

```bash
# Linux / macOS
python3 -m venv venv
source venv/bin/activate          # or: . venv/bin/activate
pip install -r requirements.txt
playwright install chromium       # browsers for the streaming extractors

# ffmpeg (Ubuntu/Debian)
sudo apt install -y ffmpeg
```

On Windows, install Python 3.10+ from [python.org](https://www.python.org/) (check *Add to
PATH* during install), then:

```bat
py -m pip install -r requirements.txt
py -m playwright install chromium
```

Install ffmpeg from [ffmpeg.org](https://ffmpeg.org/download.html) and add it to PATH, or use
`choco install ffmpeg`.

### 2. Configure

**Option A — interactive setup (recommended).** The setup script asks you interactively for
`API_ID`, `API_HASH`, `OWNER_ID` and the destination `CHANNEL_ID` and writes your `.env`
for you:

```bash
./setup.sh        # Linux / Termux
```

```bat
setup.bat         # Windows
```

**Option B — manual setup.** Copy the template and fill it in yourself:

```bash
cp .env.example .env
# then edit .env with your own values
```

You need four things:

1. **API_ID and API_HASH** — go to [my.telegram.org](https://my.telegram.org), log in with
   the userbot's phone number, open **API development tools**, and copy the App `api_id` and
   `api_hash` shown there.
2. **OWNER_ID** — your main Telegram account's numeric ID. Easiest way: send a message to
   [@RawDataBot](https://t.me/RawDataBot) or [@userinfobot](https://t.me/userinfobot) and read
   the `id` from its reply.
3. **CHANNEL_ID** — the destination channel's numeric ID where videos get posted
   (e.g. `-1001234567890`). The userbot account must be an **admin** of that channel. To find
   the ID, forward any message from the channel to [@RawDataBot](https://t.me/RawDataBot) and
   read the `id`. Alternatively set `CHANNEL_USERNAME=@yourchannel` instead.
4. **Session** — created automatically on first login (see step 3).

### 3. Start the bot

```bash
./start_bot.sh        # Linux (runs inside a `screen` session)
```

```bat
start.bat             # Windows
```

The first time you start it, Telethon will ask for the userbot's **phone number** and the
**verification code** Telegram sends you. That creates the session file
(`my_video_downloader_bot.session`); every later start is instant.

---

## Admin commands

Only you (`OWNER_ID`) can run these in a private chat with the userbot. **Send them as text**
— inline buttons don't work with userbots. For confirmation prompts, reply with `si` (`yes`).

| Command | Description |
| --------- | ------------- |
| `/adduser @username` | Add a user to the whitelist (by username) |
| `/adduser 123456789` | Add a user to the whitelist (by numeric ID) |
| `/removeuser @username` | Remove a user from the whitelist |
| `/users` | List all authorized users |
| `/channel` | Show the destination channel |
| `/status` | Show the current download and queue length |
| `/stop` | Cancel the current download/upload (and clean orphan files if idle) |
| `/queue` | Show everything in the download queue |
| `/now <url>` | Move a queued URL to the front (priority) |
| `/clean` | Clear the whole queue (asks for confirmation: reply `si`/`no`) |

**Quality selection:** after sending a link, reply `1` (360p), `2` (720p), `3` (1080p) or
`4` (MAX). For playlists, reply with the video number, or `next`/`prev` to page.

---

## How extraction works (3-level architecture)

The bot tries to get a video in three stages, from the cheapest to the most powerful:

1. **Dedicated extractors** (`bot/extractors/`). Registered per-domain, tried first. Some are
   lightweight HTTP/HTML parsers; others launch
   **headless Chromium** via Playwright to defeat Cloudflare and capture the `m3u8`/MP4 stream
   from the page's own player while the site handles its own tokens and cookies transparently.
2. **`yt-dlp`** (the default engine). For everything without a dedicated extractor —
   YouTube, TikTok, Instagram, Twitter/X, Vimeo and hundreds more. `yt-dlp` runs
   `extract_info` to get metadata and then downloads the chosen quality.
3. **Universal Playwright fallback** (`bot/extractors/universal.py`). Used *only* when
   `yt-dlp` fails (broken/outdated extractor, Cloudflare 403, etc.). It loads the page in
headless Chromium and intercepts the first stream the page itself plays — covering dozens
of video sites with no per-site code. Sites whose `yt-dlp` extractor works never pay the
browser cost.

> **Note:** the fallback (and the streaming-site extractors) need a browser installed:
> `playwright install chromium`. On environments where Playwright/Chromium is unavailable
> (e.g. Android/Termux), the bot degrades gracefully and keeps working with `yt-dlp` only.

**Send the page URL, not the player URL.** For streaming sites, forward the **movie/series
page** link, not the internal player link (e.g. `vidxgo.co` / `vidplay`). Those player URLs
only live inside an iframe; opening them directly returns 403/404 — the bot tells you to
resend the parent page.

---

## Supported sites

### Via `yt-dlp` (native)

YouTube, TikTok, Instagram, Twitter/X, Vimeo and
[hundreds more](https://github.com/yt-dlp/yt-dlp/blob/master/supportedsites.md).

### Via dedicated / universal Playwright extractors

Streaming sites protected by Cloudflare (movie/series portals and similar) plus a
universal fallback that covers dozens of video sites with no per-site code
(see `bot/extractors/universal.py`).

### Adding a new site

To add a dedicated Playwright extractor, create `bot/extractors/yoursite.py`:

```python
from bot.extractors.base import PlaywrightVideoExtractor

class MySiteExtractor(PlaywrightVideoExtractor):
    DOMAINS = ("mysite.com",)
```

Then register it in `bot/extractors/__init__.py` (`_EXTRACTORS` list). Or simply add a domain
to the universal extractor's `DOMAINS` tuple.

---

## Advanced configuration

All settings live in `.env` (see `.env.example`). Required: `API_ID`, `API_HASH`,
`OWNER_ID`, and one of `CHANNEL_ID` / `CHANNEL_USERNAME`.

| Variable | Default | Description |
| ---------- | --------- | ------------- |
| `API_ID` | — | **Required.** From [my.telegram.org](https://my.telegram.org) |
| `API_HASH` | — | **Required.** From [my.telegram.org](https://my.telegram.org) |
| `SESSION_NAME` | `my_video_downloader_bot` | Base name of the `.session` file |
| `SESSION_PATH` | empty | Full path to the session file — use this to avoid SQLite `database is locked` on shared/Samba mounts. On Linux it can be a Windows path and vice-versa; the client ignores mismatched OS paths |
| `CHANNEL_ID` | — | **Required** (unless using `CHANNEL_USERNAME`). Destination channel numeric ID, e.g. `-1001234567890` |
| `CHANNEL_USERNAME` | empty | Alternative to `CHANNEL_ID`, e.g. `@mychannel` |
| `OWNER_ID` | — | **Required.** Your Telegram numeric ID |
| `DOWNLOAD_DIR` | `downloads/` | Temporary folder for downloading files |
| `MAX_RETRIES` | `3` | Download/upload retry attempts before giving up (with exponential backoff) |
| `MAX_FILE_SIZE_GB` | `2` | Max file size, in GB. Enforced **before** downloading (when the source reports it) and re-checked after — rejects anything over the limit instead of wasting bandwidth |
| `PROGRESS_UPDATE_INTERVAL` | `2` | Target interval between progress updates (the bot also throttles edits to avoid FloodWait) |

**Network / download tuning:**

| Variable | Default | Description |
| ---------- | --------- | ------------- |
| `COOKIES_FROM_BROWSER` | empty | Browser name from which to load YouTube cookies: `chrome` \| `edge` \| `firefox` \| `brave` \| `chromium` \| `opera` \| `vivaldi` \| `whale`. **Fixes YouTube's "Sign in to confirm you're not a bot"** |
| `COOKIES_FILE` | `data/cookies.txt` | Path to a `cookies.txt` (export with a "Get cookies.txt LOCALLY" browser extension). Takes priority over `COOKIES_FROM_BROWSER` |
| `FORCE_IPV4` | `true` | Force IPv4 connections — recommended on Linux to avoid YouTube/Cloudflare blocks |
| `USE_ARIA2` | `true` | Use the multi-threaded `aria2c` for direct HTTP/FTP downloads (helpful on weak CPUs). Falls back to the native downloader if `aria2c` isn't installed |
| `CONCURRENT_FRAGMENTS` | `24` | Number of fragments downloaded in parallel for HLS/DASH. Lower to 3–8 on weak CPUs or slow connections |
| `HTTP_CHUNK_SIZE` | empty | HTTP chunk size for the native yt-dlp downloader (speeds up single-file CDN pulls); e.g. `10485760` for 10 MB |
| `IMPERSONATE` | `chrome` | TLS fingerprint impersonation via `curl_cffi` (set to `none`/`off` to disable). Defeats Cloudflare/Akamai 403 blocks |

**Slow-start detector:**

| Variable | Default | Description |
|----------|---------|-------------|
| `SLOW_START_GRACE` | `12` | Seconds to observe a download before judging it "slow". If it stays below `SLOW_START_MIN_KB` KB/s for this long, the bot restarts with more concurrent connections |
| `SLOW_START_MIN_KB` | `250` | Minimum sustained throughput (KB/s) required during the grace period to avoid a slow-start restart |

**Playwright:**

| Variable | Default | Description |
| ---------- | --------- | ------------- |
| `PLAYWRIGHT_BROWSERS_PATH` | default cache | Where Playwright looks for its browsers (defaults to `~/.cache/ms-playwright` on Linux, `%LOCALAPPDATA%\ms-playwright` on Windows). Set if you installed Chromium elsewhere |
| `PLAYWRIGHT_SHARED_BROWSER` | `1` | Reuse one Chromium process across extractions (faster, less RAM). Set to `0` for one browser per extraction |
| `LOCAL_API_PORT` | `8091` | Local control API (localhost only) for dashboards/tooling: queue, progress, cancel. Set to `0` to disable |
| `LOCAL_API_HOST` | `127.0.0.1` | Bind address for the local API (loopback only — never exposed) |
| `LOCAL_API_TOKEN` | auto-generated | Bearer token for the local API. If empty, one is generated and stored in `data/.local_api_token` |

---

## Content policy

This bot downloads **only content that is freely and publicly available from its source** —
the same video any anonymous visitor can watch in their browser.

- It does **not** bypass paywalls, membership-only content, private videos, DRM, or any
  access-restricted material.
- When a video is detected as members-only, paywalled, private, geo-blocked, or otherwise not
  freely available, the bot **refuses it with a clear, explicit message** instead of trying to
  circumvent the restriction.

You are responsible for making sure you have the right to download any content you use this
software to obtain, and for complying with the terms of service of the sites you use it with.

---

## Deploy

### systemd (Linux server)

Create `/etc/systemd/system/videobot.service`:

```ini
[Unit]
Description=Telegram Video Downloader Userbot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=ersi                      # the user that owns the repo
WorkingDirectory=/home/ersi/telegram-video-downloader-userbot
ExecStart=/home/ersi/telegram-video-downloader-userbot/venv/bin/python run.py
Restart=always
RestartSec=5
Environment=OPENSSL_CONF=/dev/null

[Install]
WantedBy=multi-user.target
```

Then enable and start it:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now videobot
sudo systemctl status videobot
journalctl -u videobot -f      # watch live logs
```

### screen

Without systemd, run the bot in a detached `screen` session:

```bash
screen -dmS videobot ./venv/bin/python run.py
screen -r videobot             # attach to see logs (Ctrl+A, D to detach)
screen -ls                     # list sessions
```

`start_bot.sh` already does this for you.

### Android / Termux

Turn an old Android phone into a free 24/7 host:

1. Install **Termux from F-Droid** (not the Play Store — it's outdated):
   <https://f-droid.org/packages/com.termux/>
2. Open Termux and run:

   ```bash
   pkg update -y && pkg install -y git
   git clone https://github.com/ErsiZyka/telegram-video-downloader-userbot.git
   cd telegram-video-downloader-userbot
   bash setup_termux.sh
   ```

3. Edit `.env` with your values (`nano .env`).
4. Either copy your `my_video_downloader_bot.session` from your PC to the bot folder, or log
   in on first start.
5. Start with `bash start_termux.sh` — it automatically engages `termux-wake-lock` so Android
   doesn't kill the process.

For auto-start on phone reboot, install **Termux:Boot** (F-Droid) and:

```bash
mkdir -p ~/.termux/boot
cp start_termux.sh ~/.termux/boot/run-bot.sh
```

> **Note:** The dedicated streaming-site extractors need desktop Chromium, which **does not
> run on Android** — those features won't work on Termux. YouTube, TikTok, Instagram, and all
> other `yt-dlp` sites work normally.

---

## Troubleshooting

| Problem | Fix |
| --------- | ----- |
| **`yt-dlp non è installato` / "yt-dlp not installed" at startup** | `pip install yt-dlp` (or reinstall `requirements.txt`). The bot checks at startup and exits otherwise |
| **`ffmpeg non è installato` / "ffmpeg not installed"** | Install ffmpeg + ffprobe (Ubuntu: `sudo apt install ffmpeg`; Windows: ffmpeg.org or `choco install ffmpeg`). The bot refuses to start without it |
| **"Sign in to confirm you're not a bot" on YouTube** | Set `COOKIES_FROM_BROWSER=chrome` (or `edge`/`firefox`/...) in `.env` to the browser where you're logged into YouTube, or export cookies to a file and set `COOKIES_FILE` |
| **`BrowserType.launch` / Chromium not installed** | Run `playwright install chromium`. The streaming-site and universal extractors need it |
| **Video reserved for members / "contenuto a pagamento"** | This is **intentional**. The bot only downloads freely available content and refuses paywalled/members-only material with an explicit message — see [Content policy](#content-policy) |
| **"File too large" / over 2 GB** | Telegram user accounts cap uploads at **2 GB** (4 GB with Premium). The bot rejects larger files. Use a lower quality or a smaller video |
| **`FloodWaitError` / "A wait of X seconds is required"** | Telegram rate-limiting. The bot mutes status-message edits during the wait and keeps logging progress locally, then resumes automatically — just be patient |
| **HTTP 403 Forbidden on a site** | The bot retries with realistic browser headers and/or TLS impersonation (`IMPERSONATE=chrome`). Make sure `FORCE_IPV4=true` on Linux |
| **Slow downloads** | Likely the CDN throttling (e.g. ~0.5 MB/s on some streaming CDNs), not the bot. The slow-start detector boosts concurrent fragments automatically; try `720p` for the best speed/quality balance |
| **`my_video_downloader_bot.session` already exists / database locked** | Set `SESSION_PATH` to a path on the local filesystem (not a shared/Samba mount) |

---

## Testing

```bash
python -m pytest tests/ -v
```

The suite (130+ tests) covers the downloader, queue, whitelist, extractor registry,
handlers, the local control API, per-job progress, the upload-size cap and the full
link → menu → download flow.

---

## Project structure

```
telegram-video-downloader-userbot/
├── .env.example              # Configuration template
├── run.py                    # Entry point
├── requirements.txt          # Loose dependencies (pip install)
├── requirements.lock         # Pinned versions (reproducible installs)
├── start_bot.sh              # Linux start (screen)
├── start.bat                 # Windows start
├── start_with_log.bat        # Windows start + live log window
├── log.bat                   # Windows live log tail
├── status_bot.sh             # Show bot status (processes/log/warp)
├── setup_termux.sh           # Android (Termux) setup
├── start_termux.sh           # Android start (with wake-lock)
├── install_warp.sh           # Optional: Cloudflare WARP helper
├── bot/
│   ├── client.py             # Telethon client factory (session isolation)
│   ├── api.py                # Local control API (localhost only, for dashboards)
│   ├── progress.py           # Per-job progress + cancel registries
│   ├── handlers.py           # Message handlers, menus, queue worker, admin commands
│   ├── downloader.py         # yt-dlp wrapper, retry, progress, slow-start, ffprobe
│   ├── fasttelethon.py       # Parallel chunk upload (FastTelethon)
│   ├── history.py            # Persistent download history (dedup)
│   ├── queue.py              # Persistent FIFO queue (crash-safe)
│   ├── logging_config.py     # Centralized logging (console + rotating file)
│   ├── whitelist.py          # Authorized-users gatekeeper
│   └── extractors/
│       ├── __init__.py       # Extractor registry
│       ├── base.py           # Base + PlaywrightVideoExtractor (shared browser)
│       ├── <site>.py         # One module per supported site
│       └── universal.py      # Multi-site Playwright fallback
├── data/                     # Runtime JSON/session/logs (gitignored)
├── downloads/                # Temp download dir (gitignored)
└── tests/                    # Pytest suite (downloader, queue, api, flow, …)
```

---

## Contributing

Contributions are welcome! Please:

1. Fork the repository and create a feature branch.
2. Add or update tests for any behavior you change.
3. Run the test suite (`python -m pytest`) before opening a pull request.
4. Keep changes focused on a single concern (a new extractor, a bug fix, a new feature).

If you add a site extractor, remember to register it in `bot/extractors/__init__.py`.

---

## License

[MIT](LICENSE). The bundled `bot/fasttelethon.py` is adapted from
[mautrix-telegram](https://github.com/tulir/mautrix-telegram) (© 2021 Tulir Asokan), MIT
licensed. Third-party components — `yt-dlp`, `Telethon`, `Playwright` — remain under their
own respective licenses.

---

## ServerPanel — optional web dashboard

The repo includes a small **web control panel** (`panel.py` + `templates/panel.html`) to monitor
and drive the server from a browser (phone or PC):

- **Dashboard**: bot status, load, RAM/disk usage bars, uptime.
- **Processes**: live list of user processes with CPU/mem, search + one-click kill.
- **Docker**: every container with start/stop/restart.
- **Terminal**: free shell box (`systemctl`, `docker`, scripts…) with a colored live view.
- **Live logs**: real-time tail of `data/bot.log` over WebSocket (pause/clear controls).
- **Power**: reboot or power-off the server straight from the UI.

It is password-protected and intended to run inside a **private network** (LAN or **Tailscale**).

```bash
pip install fastapi "uvicorn[standard]" jinja2
# create panel.env with: PANEL_PASSWORD=your_password
sudo cp panel.service /etc/systemd/system/ && sudo systemctl enable --now panel
# sudo NOPASSWD is REQUIRED for the panel's control buttons (bot/docker/reboot) and for
# sudo commands in the shell box:
echo "$(whoami) ALL=(ALL) NOPASSWD: ALL" | sudo tee /etc/sudoers.d/90-panel-$(whoami)
sudo chmod 440 /etc/sudoers.d/90-panel-$(whoami)
# open http://<server>:8080  (over Tailscale: http://<hostname>:8080 from any device)
```
