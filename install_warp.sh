#!/bin/bash
# install_warp.sh v3 — Usa repo Cloudflare con codename Ubuntu LTS stabile (noble)
set -e

PASS="$1"

run_sudo() {
    echo "$PASS" | sudo -S "$@"
}

echo "=== 1. Aggiungo repo Cloudflare (con codename noble per compatibilità) ==="
# Scarica e dearmor la chiave GPG
wget -q https://pkg.cloudflareclient.com/pubkey.gpg -O /tmp/cf-key.gpg
run_sudo mkdir -p /usr/share/keyrings
# Converti in formato binario con gpg --dearmor (o usa gpg direttamente)
run_sudo bash -c 'gpg --dearmor < /tmp/cf-key.gpg > /usr/share/keyrings/cloudflare-warp-archive-keyring.gpg 2>/dev/null'
# Fallback: se gpg --dearmor non funziona, copia il file così com'è
if [ ! -s /usr/share/keyrings/cloudflare-warp-archive-keyring.gpg ]; then
    run_sudo cp /tmp/cf-key.gpg /usr/share/keyrings/cloudflare-warp-archive-keyring.gpg
fi

# Usa "noble" (Ubuntu 24.04) come codename — compatibile con 26.04
echo "deb [signed-by=/usr/share/keyrings/cloudflare-warp-archive-keyring.gpg] https://pkg.cloudflareclient.com/ noble main" | run_sudo tee /etc/apt/sources.list.d/cloudflare-client.list > /dev/null

echo "=== 2. Aggiorno pacchetti ==="
run_sudo apt update -qq 2>&1 | tail -5

echo "=== 3. Installo cloudflare-warp ==="
run_sudo apt install -y cloudflare-warp 2>&1

echo "=== 4. Verifico installazione ==="
if command -v warp-cli &>/dev/null; then
    echo "✅ warp-cli installato"
    warp-cli --version 2>/dev/null || true
else
    echo "❌ warp-cli non trovato! Tento con nome alternativo 'warp-svc'..."
    run_sudo apt install -y warp-svc 2>&1 || true
    if ! command -v warp-cli &>/dev/null; then
        echo "❌ Installazione fallita"
        exit 1
    fi
fi

echo "=== 5. Avvio demone warp-svc ==="
run_sudo systemctl enable warp-svc --now 2>/dev/null || run_sudo service warp-svc start 2>/dev/null || true
sleep 3

echo "=== 6. Registrazione WARP (accetta termini) ==="
# warp-cli registration register è interattivo, usa --accept-tos se disponibile
warp-cli registration new --accept-tos 2>&1 || \
warp-cli register --accept-tos 2>&1 || \
warp-cli register 2>&1 || \
yes | warp-cli registration register 2>&1 || \
echo "⚠️ Registrazione non riuscita o già registrato"

sleep 2

echo "=== 7. Connessione WARP ==="
warp-cli connect 2>&1 && echo "✅ Connesso!" || echo "⚠️ Connessione fallita"

sleep 4

echo "=== 8. Stato WARP ==="
warp-cli status 2>&1

echo ""
echo "=== 9. Verifica IP pubblico ==="
echo "Tento curl su 1.1.1.1..."
curl -s --max-time 10 https://1.1.1.1/cdn-cgi/trace 2>&1 | grep -E 'warp|ip' || echo "(curl non disponibile o WARP non attivo)"
echo ""
echo "=== INSTALLAZIONE WARP COMPLETATA ==="
