#!/usr/bin/env bash
# Retire l'ancien Hermes (Node/systemd) et EFFACE ses données. Tourne SUR le VPS, en root, poussé par
# `ssh ... bash -s`. Ne touche pas à /opt/okxq. Ne s'exécute que si OKXQ_CONFIRM_WIPE vaut EFFACER.
set -u
if [ "${OKXQ_CONFIRM_WIPE:-}" != "EFFACER" ]; then
  echo "refus : OKXQ_CONFIRM_WIPE doit valoir EFFACER"; exit 1
fi
echo "=== services hermes* ==="
for u in $(systemctl list-unit-files --no-legend 'hermes*' 2>/dev/null | awk '{print $1}'); do
  systemctl disable --now "$u" >/dev/null 2>&1 || true
  rm -f "/etc/systemd/system/$u"
  echo "  $u retiré"
done
for u in hermes-research.timer hermes-research.service hermes-dashboard.service hermes-perles.timer hermes-perles.service hermes.service; do
  rm -f "/etc/systemd/system/$u"
done
systemctl daemon-reload
pkill -f "node app/main.js" 2>/dev/null || true
pkill -f "deploy/chercher_perles.js" 2>/dev/null || true
sleep 1
echo "=== dossiers et données ==="
for d in /root/hermes /root/hermes_v4 /root/hermes-v4 /root/HERMES_V4_LIVE /opt/hermes /root/astra /root/incoming /root/sauvegardes; do
  if [ -e "$d" ]; then
    du -sh "$d" 2>/dev/null | sed 's/^/  effacé : /'
    rm -rf "$d"
  fi
done
rm -rf /root/.npm 2>/dev/null || true
rm -rf /root/.cache/node* 2>/dev/null || true
echo "=== ce qui reste dans /root ==="
ls -la /root | sed 's/^/   /'
echo "=== disque ==="
df -h / | tail -1
echo "ancien Hermes retiré, données effacées"
