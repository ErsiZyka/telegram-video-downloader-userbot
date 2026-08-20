@echo off
rem setup.bat — Interactive first-time configuration (Windows).
rem Run it once: it asks for your Telegram API credentials and writes .env for you.

setlocal enabledelayedexpansion
cd /d "%~dp0"

echo ==============================================
echo  Telegram Video Downloader Userbot — Setup
echo ==============================================
echo.
echo You need:
echo   1. API_ID and API_HASH  -^> from https://my.telegram.org (API development tools)
echo   2. CHANNEL_ID           -^> the channel where videos will be posted
echo   3. OWNER_ID             -^> your Telegram user ID (the one who controls the bot)
echo.

set /p API_ID="API_ID (number from my.telegram.org): "
set /p API_HASH="API_HASH (hash from my.telegram.org): "

echo.
echo Channel: use the numeric ID (e.g. -1001234567890) or the username (e.g. @mychannel).
echo How to find it: forward a message from the channel to @userinfobot on Telegram.
set /p CHANNEL="CHANNEL_ID or CHANNEL_USERNAME: "

set /p OWNER_ID="OWNER_ID (your Telegram user ID, from @userinfobot): "

echo.
set /p BROWSER="Browser for YouTube cookies [chrome, firefox, edge, or empty to skip]: "

if exist .env (
    set /p OW=".env already exists. Overwrite? [y/N]: "
    if /i not "!OW!"=="y" (
        echo Aborted. Keeping existing .env.
        exit /b 0
    )
)

> .env (
    echo # Credentials from https://my.telegram.org
    echo API_ID=!API_ID!
    echo API_HASH=!API_HASH!
    echo.
    echo # Session file name
    echo SESSION_NAME=my_video_downloader_bot
    echo SESSION_PATH=
    echo.
    if "!CHANNEL:~0,1!"=="@" (
        echo # Destination channel
        echo CHANNEL_USERNAME=!CHANNEL!
    ) else (
        echo # Destination channel
        echo CHANNEL_ID=!CHANNEL!
    )
    echo.
    echo # Owner ^(your Telegram user ID^)
    echo OWNER_ID=!OWNER_ID!
    echo.
    echo # Download settings
    echo DOWNLOAD_DIR=downloads/
    echo MAX_RETRIES=3
    echo MAX_FILE_SIZE_GB=2
    echo PROGRESS_UPDATE_INTERVAL=2
    echo.
    echo # Network
    echo FORCE_IPV4=true
    echo USE_ARIA2=true
    echo CONCURRENT_FRAGMENTS=16
    echo IMPERSONATE=chrome
    echo.
    if not "!BROWSER!"=="" (
        echo # YouTube cookies ^(fixes "Sign in to confirm you're not a bot"^)
        echo COOKIES_FROM_BROWSER=!BROWSER!
        echo COOKIES_FILE=data/cookies.txt
    )
)

echo.
echo Done! .env written.
echo Now install dependencies and start the bot:
echo   pip install -r requirements.txt
echo   start.bat
