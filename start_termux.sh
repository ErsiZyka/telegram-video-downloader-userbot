#!/data/data/com.termux/files/usr/bin/bash
# =============================================================================
# Avvio del bot su Termux con wake-lock (mantiene il telefono sveglio)
# =============================================================================
# Uso:  bash start_termux.sh
# Stop: Ctrl+C  (rilascia il wake-lock automaticamente)
# =============================================================================

# Imposta la cartella del bot (modifica se hai clonato altrove)
cd "$(dirname "$0")" || exit 1

echo "================================================"
echo "  Avvio bot su Termux"
echo "================================================"

# Wake-lock: impedisce ad Android di mettere in sleep il telefono e
# di uccidere il processo in background. Rilasciato all'uscita.
termux-wake-lock 2>/dev/null || echo "(termux-wake-lock non disponibile: il telefono potrebbe addormentarsi)"
trap 'termux-wake-unlock 2>/dev/null; echo "Wake-lock rilasciato. Bot fermato."' EXIT

# Permette connessioni in background (es. SSH per gestione remota opzionale)
# termux-wake-lock richiede che lo schermo sia acceso almeno una volta dopo il boot.

# Avvia il bot
python run.py
