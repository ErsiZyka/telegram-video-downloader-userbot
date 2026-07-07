#!/data/data/com.termux/files/usr/bin/bash
# =============================================================================
# Setup del bot video downloader su Android (Termux)
# =============================================================================
# Trasforma il telefono in un "VPS" per tenere attivo il bot 24/7 gratis.
#
# Come usarlo:
#   1. Installa Termux da F-Droid (NON dal Play Store, è obsoleto)
#      https://f-droid.org/packages/com.termux/
#   2. Apri Termux e dai:
#        pkg update -y && pkg install -y git
#        git clone https://github.com/ErsiZyka/telegram-video-downloader-userbot.git
#        cd telegram-video-downloader-userbot
#        bash setup_termux.sh
#   3. Copia il file .session dal PC (vedi sotto) o fai il login dal telefono
#   4. Avvia con: bash start_termux.sh
#
# Copiare la sessione dal PC (per non rifare il login):
#   Sul PC:  scp my_video_downloader_bot.session <telefono>:~/telegram-video-downloader-userbot/
#   Oppure invialo al telefono (Telegram) e salvalo nella cartella del bot.
#
# Per auto-avvio al riavvio del telefono: installa Termux:Boot
#   https://f-droid.org/packages/com.termux.boot/  poi vedi start_termux.sh
# =============================================================================

set -e

echo "================================================"
echo "  Setup bot video downloader su Termux (Android)"
echo "================================================"

# --- 1. Pacchetti di sistema Termux ---
echo ""
echo "[1/5] Installazione pacchetti di sistema..."
pkg update -y
pkg install -y python ffmpeg git openssh termux-api termux-tools

# --- 2. Python: dipendenze del bot ---
echo ""
echo "[2/5] Installazione dipendenze Python..."
pip install --upgrade pip
# Playwright su Android/Termux: il pacchetto pip si installa, ma il browser
# Chromium precompilato NON gira su ARM Android. Lo installiamo comunque per
# non rompere l'import, ma gli estrattori streaming italiani NON funzioneranno
# sul telefono (YouTube, TikTok, ecc. con yt-dlp sì). Vedi README sezioni limiti.
pip install -r requirements.txt || {
    echo "pip install fallito in parte. Riprovo senza playwright..."
    pip install telethon cryptg yt-dlp python-dotenv
}

# --- 3. Configurazione .env ---
echo ""
echo "[3/5] Configurazione .env..."
if [ ! -f .env ]; then
    if [ -f .env.example ]; then
        cp .env.example .env
        echo "Creato .env da template. EDITALO con i tuoi dati:"
        echo "    nano .env   (o: pkg install nano)"
        echo "Valori richiesti: API_ID, API_HASH, CHANNEL_ID, OWNER_ID"
    fi
else
    echo ".env già presente, lo lascio così."
fi

# --- 4. Directory dati ---
echo ""
echo "[4/5] Creazione directory..."
mkdir -p data downloads

# --- 5. Controllo sessione ---
echo ""
echo "[5/5] Controllo sessione Telegram..."
if [ -f my_video_downloader_bot.session ]; then
    echo "Sessione trovata: OK"
else
    echo "Nessuna sessione trovata."
    echo "  Opzione A: al primo avvio (start_termux.sh) fai il login da qui"
    echo "  Opzione B: copia my_video_downloader_bot.session dal PC"
fi

echo ""
echo "================================================"
echo "  SETUP COMPLETATO"
echo "================================================"
echo ""
echo "Prossimi passi:"
echo "  1. Editare .env (se non fatto):  nano .env"
echo "  2. Avviare il bot:                bash start_termux.sh"
echo ""
echo "Mantenere acceso (anti-sleep):"
echo "  Il bot usa termux-wake-lock automaticamente (vedi start_termux.sh)."
echo "  Per bloccarlo:  termux-wake-unlock"
echo ""
echo "Auto-avvio al riavvio del telefono:"
echo "  Installa Termux:Boot (F-Droid), apri l'app una volta, poi:"
echo "  mkdir -p ~/.termux/boot"
echo "  cp start_termux.sh ~/.termux/boot/run-bot.sh"
echo ""
echo "Accedere da PC via SSH (opzionale, per gestirlo da remoto):"
echo "  passwd   # imposta una password"
echo "  sshd     # avvia il server SSH (porta 8022)"
echo "  # dal PC: ssh -p 8022 <ip-telefono>"
echo ""
echo "NOTA: gli estrattori streaming italiani (altadefinizione, streamingcommunity)"
echo "richiedono Chromium headless che NON gira su Android. yt-dlp (YouTube,"
echo "TikTok, Instagram, YouPorn, ecc.) funziona normalmente."
