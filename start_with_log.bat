@echo off
REM Avvia il bot E una finestra log separata che mostra tutto in tempo reale.
cd /d "C:\Users\ersi\video_downloader_bot"

echo Avvio finestra log in tempo reale...
start "Log Bot (live)" log.bat

echo Avvio del bot...
timeout /t 2 >nul
C:\Users\ersi\AppData\Local\Programs\Python\Python312\python.exe run.py
pause
