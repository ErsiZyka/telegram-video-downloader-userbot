#!/bin/bash
# status_bot.sh — Mostra lo stato del bot Telegram
cd /home/ersi/condivisa/video_downloader_bot

echo "=== Processi bot ==="
ps aux | grep run.py | grep -v grep || echo "NESSUN BOT ATTIVO"

echo ""
echo "=== Servizio systemd ==="
if systemctl is-active --quiet videobot.service 2>/dev/null; then
    echo "videobot.service: ATTIVO"
    systemctl show videobot.service -p MainPID --value | xargs -I{} ps -p {} -o pid=,etime=,cmd=
else
    echo "videobot.service: NON ATTIVO"
fi

echo ""
echo "=== WARP ==="
curl -s --max-time 5 https://1.1.1.1/cdn-cgi/trace | grep -E 'warp|ip'

echo ""
echo "=== Ultimo log ==="
tail -10 data/bot.log
