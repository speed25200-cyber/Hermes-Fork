#!/usr/bin/env bash
# Arrête les moteurs précédents (plateforme okxq sous Docker, ancien service Node « hermes »).
# Ne ferme AUCUNE position chez OKX : le nouveau moteur les verra et les gérera s'il trade ces contrats.
set -u
if [ -d /opt/okxq ] && command -v docker >/dev/null; then
  (cd /opt/okxq && docker compose down 2>/dev/null) && echo "okxq arrêté"
fi
for unit in hermes.service hermes-live.service okxq.service; do
  if systemctl list-unit-files "$unit" >/dev/null 2>&1 && systemctl is-enabled --quiet "$unit" 2>/dev/null; then
    systemctl disable --now "$unit" && echo "$unit arrêté"
  fi
done
pgrep -af "node .*app/main.js" && pkill -f "node .*app/main.js" && echo "processus Node arrêté" || true
echo "anciens moteurs retirés (positions éventuelles laissées ouvertes chez l'exchange)"
