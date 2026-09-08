
## Website — Video Site (LAN)

Browser frontend with the same functions as the bot, plus multi-channel support:

- **Download** — paste one or more links, pick channel and quality, live progress
- **Channels** — link new channels via ID or username, per-download destination
- **Queue** — live status, prioritize, remove, clear, stop
- **History** — recent successes and errors with retry
- **File upload** — send a video from your device straight to a channel

It runs on your own machine and is reachable **only on your home network** through a
secret link: everything lives under the /s/TOKEN/ prefix, anything else is a 404.

    # .env
    SITE_TOKEN=your_long_random_token
    SITE_PORT=8090
    sudo systemctl enable --now videosite
    # open http://server:8090/s/TOKEN/

The site talks to the bot only through its localhost Local API (bot/api.py) — it never
touches the Telegram session or the queue files. The bot must be running (videobot).
