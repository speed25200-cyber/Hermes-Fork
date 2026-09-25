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
echo "== calendrier Binance (12 derniers contrats USDT lancés, tous types)"
/opt/hermes/.venv/bin/python - <<'PY' 2>&1 | tail -40
import collections, json, time, urllib.request
info = json.load(urllib.request.urlopen("https://fapi.binance.com/fapi/v1/exchangeInfo", timeout=20))
syms = [s for s in info["symbols"] if s.get("quoteAsset") == "USDT"]
for s in sorted(syms, key=lambda s: s.get("onboardDate", 0))[-12:]:
    t = time.strftime("%Y-%m-%d %H:%M", time.gmtime(s.get("onboardDate", 0) / 1000))
    print(f"  {t}  {s['symbol']:<16} {str(s.get('contractType')):<18} {str(s.get('status')):<16} {s.get('underlyingType')}")
kinds = collections.Counter((s.get("contractType"), s.get("underlyingType")) for s in syms)
print("  types :", ", ".join(f"{k[0]}/{k[1]} {n}" for k, n in kinds.most_common()))
print("  heure serveur :", time.strftime("%Y-%m-%d %H:%M", time.gmtime(info.get("serverTime", 0) / 1000)))
PY
echo "== tableau de bord"
systemctl --no-pager --plain list-units 'hermes-dashboard*' 2>/dev/null | head -5
journalctl -u 'hermes-dashboard@*' -n 8 --no-pager 2>/dev/null | tail -8
TOKEN=$(sed -n 's/^HERMES_DASHBOARD_TOKEN=//p' /etc/hermes/hermes.env 2>/dev/null | tail -1)
PORT=$(sed -n 's/^HERMES_DASHBOARD_PORT=//p' /etc/hermes/hermes.env 2>/dev/null | tail -1)
PORT=${PORT:-8899}
if [ -n "$TOKEN" ]; then
  for u in / /static/app.js /api/modes "/api/snapshot?mode=paper" "/api/candles?symbol=BTCUSDT&tf=30m"; do
    code=$(curl -s -o /tmp/hd.$$ -w "%{http_code}" -H "Authorization: Bearer $TOKEN" "http://127.0.0.1:$PORT$u")
    echo "  $u -> $code ($(wc -c < /tmp/hd.$$) octets)"
  done
  echo "  bougies : $(head -c 160 /tmp/hd.$$)"
  rm -f /tmp/hd.$$
fi
unset TOKEN
