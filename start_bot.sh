#!/bin/bash
# start_bot.sh — Avvia il bot Telegram sul server Linux
cd /home/ersi/condivisa/video_downloader_bot || exit 1

echo "=== Pulizia vecchi processi ==="
pkill -9 -f 'python.*run.py' 2>/dev/null
screen -wipe 2>/dev/null
sleep 1

echo "=== Avvio bot in screen ==="
export OPENSSL_CONF=/dev/null
screen -dmS vdb ./venv/bin/python3 run.py
sleep 5

echo "=== Log ==="
tail -8 data/bot.log

echo ""
echo "=== Processi ==="
ps aux | grep run.py | grep -v grep || echo "NESSUN BOT"

echo ""
echo "=== Screen ==="
screen -ls | grep vdb || echo "SCREEN NON TROVATA"
