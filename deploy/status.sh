#!/usr/bin/env bash
# État du VPS : services, dernier état publié par le moteur, derniers journaux, derniers rapports.
set -u
echo "== services"
systemctl --no-pager --plain list-units 'hermes*' 2>/dev/null | head -20
systemctl --no-pager list-timers hermes-retrain.timer 2>/dev/null | head -3
for m in paper demo live; do
  f=/opt/hermes/state/$m/status.json
  if [ -f "$f" ]; then echo "== état $m"; cat "$f"; echo; fi
done
for m in paper demo live; do
  if systemctl is-active --quiet "hermes@$m"; then
    echo "== journal hermes@$m"; journalctl -u "hermes@$m" -n 40 --no-pager | sed -E 's/(OKX_API_[A-Z]+=)[^ ]+/\1***/g'
  fi
done
echo "== entraînement"
journalctl -u hermes-retrain -n 25 --no-pager 2>/dev/null | tail -25
ls -1t /opt/hermes/reports/auto 2>/dev/null | head -3 | while read -r d; do
  echo "== rapport $d"; head -40 "/opt/hermes/reports/auto/$d/REPORT.md" 2>/dev/null
done
