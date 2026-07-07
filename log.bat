@echo off
REM Finestra log in tempo reale del bot.
REM Mostra data/bot.log aggiornandosi ogni secondo; Ctrl+C per chiudere.
cd /d "C:\Users\ersi\video_downloader_bot"
title Log Bot Video Downloader (live)
echo === Log live del bot (data\bot.log) ===
echo === Aggiornamento automatico ogni 1s. Premi Ctrl+C per uscire. ===
echo.
powershell -NoProfile -Command "Get-Content -Path 'data\bot.log' -Wait -Tail 50"
pause
