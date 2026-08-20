#!/bin/bash
# status_bot.sh — Mostra lo stato del bot Telegram
cd /home/ersi/condivisa/video_downloader_bot

echo "=== Processi bot ==="
ps aux | grep run.py | grep -v grep || echo "NESSUN BOT ATTIVO"

echo ""
echo "=== Screen session ==="
screen -ls | grep vdb || echo "nessuna screen attiva"

echo ""
echo "=== WARP ==="
curl -s --max-time 5 https://1.1.1.1/cdn-cgi/trace | grep -E 'warp|ip'

echo ""
echo "=== Ultimo log ==="
tail -10 data/bot.log
