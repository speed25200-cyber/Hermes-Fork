#!/usr/bin/env bash
# Relevé AVANT migration, sur le VPS. Répond à la question qui compte : y a-t-il des positions ouvertes ?
# Sortie : lignes lisibles + une ligne finale `PREFLIGHT_POSITIONS=<n>` lue par le workflow.
set -u
n=0
if [ -f /root/hermes/.env ] && systemctl is-active hermes >/dev/null 2>&1; then
  TOK=$(grep "^HERMES_DASH_TOKEN=" /root/hermes/.env | tail -1 | cut -d= -f2-)
  if [ -n "$TOK" ]; then
    r=$(curl -s --max-time 8 -X POST "http://127.0.0.1:8899/api/fetch-portfolio?key=$TOK" -H 'Content-Type: application/json' -d '{}' || echo "")
    n=$(printf '%s' "$r" | node -e '
      let b=""; process.stdin.on("data",c=>b+=c); process.stdin.on("end",()=>{
        try { const j=JSON.parse(b); const ps=(j.data&&j.data.openPositionsDetails)||[];
          for (const p of ps) console.error("  position ouverte : "+p.symbol+" "+p.side+" taille "+p.size+" pnl "+p.unrealizedPnl);
          console.log(ps.length); } catch { console.log(0); } });' 2>&1 | tee /dev/stderr | tail -1)
    grep -q "^OKX_API_KEY=.\+" /root/hermes/.env && echo "  clés OKX présentes dans l'ancien .env (elles ne seront PAS reprises)" || echo "  aucune clé OKX dans l'ancien .env"
  else
    echo "  ancien Hermes sans clé de console : positions non lisibles depuis la page"
  fi
else
  echo "  ancien Hermes inactif ou absent"
fi
echo "=== services hermes ==="; systemctl list-units --no-legend 'hermes*' 2>/dev/null || true
echo "=== disque ==="; df -h / | tail -1
echo "=== docker ==="; command -v docker >/dev/null 2>&1 && docker --version || echo "  absent (sera installé)"
echo "PREFLIGHT_POSITIONS=${n:-0}"
