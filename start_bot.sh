#!/bin/bash
# start_bot.sh — Avvia/riavvia il bot tramite il servizio systemd videobot.service
#
# NOTA STORICA: prima il bot girava in una sessione screen avviata da questo
# script, ma conviveva anche con il servizio systemd videobot.service
# (Restart=always): ogni riavvio manuale creava una DOPPIA ISTANZA che
# condivideva la sessione SQLite di Telethon → "database is locked" e crash.
# Ora systemd è l'unico gestore del bot: questo script si limita a riavviare
# il servizio (chiederà la password di sudo).

if systemctl is-active --quiet videobot.service 2>/dev/null; then
    echo "ℹ️  Il bot è già attivo (videobot.service): lo riavvio..."
fi

exec sudo systemctl restart videobot.service
