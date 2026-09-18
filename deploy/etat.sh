#!/usr/bin/env bash
# État de la plateforme sur le VPS (poussé par `ssh ... bash -s`). N'imprime aucun secret.
set -u
DIR=/opt/okxq
PORT=8899
echo "===== conteneurs ====="
cd "$DIR" 2>/dev/null && docker compose ps 2>/dev/null || echo "  /opt/okxq absent ou compose indisponible"
echo
echo "===== santé ====="
echo -n "  /health/live  : "; curl -s -o /dev/null -w "%{http_code}\n" --max-time 6 "http://127.0.0.1:${PORT}/health/live" || echo injoignable
echo -n "  /health/ready : "; curl -s -o /dev/null -w "%{http_code}\n" --max-time 6 "http://127.0.0.1:${PORT}/health/ready" || echo injoignable
echo -n "  / sans clé    : "; curl -s -o /dev/null -w "%{http_code}\n" --max-time 6 "http://127.0.0.1:${PORT}/" || echo injoignable
echo
echo "===== mode et clés (présence seulement) ====="
for f in api gateway jev-worker; do
  if [ -f "$DIR/env/$f.env" ]; then
    echo "  $f.env : $(cut -d= -f1 "$DIR/env/$f.env" | tr '\n' ' ')"
  else
    echo "  $f.env : absent"
  fi
done
grep -h "^OKXQ_MODE=" "$DIR/env/api.env" 2>/dev/null | sed 's/^/  /' || true
echo
echo "===== statut système (API locale, avec la clé du serveur, jamais imprimée) ====="
if [ -f "$DIR/env/api.env" ]; then
  TOK=$(grep "^OPERATOR_AUTH_SECRET=" "$DIR/env/api.env" | tail -1 | cut -d= -f2-)
  curl -s --max-time 8 "http://127.0.0.1:${PORT}/api/v1/system/status?key=$TOK" | head -c 1500; echo
fi
echo
echo "===== journaux (30 dernières lignes de chaque service) ====="
cd "$DIR" 2>/dev/null && docker compose logs --tail=30 --no-color 2>/dev/null | tail -120 || true
echo
echo "===== disque et mémoire ====="
df -h / | tail -1
# Un disque plein ne casse pas la plateforme tout de suite : il casse le PROCHAIN déploiement, et
# le message d'erreur ne parlera pas de disque. On le dit tant qu'il est encore temps.
LIBRE=$(df -BM --output=avail / | tail -1 | tr -dc "0-9")
if [ "${LIBRE:-0}" -lt 3000 ]; then
  echo "  !! ATTENTION : ${LIBRE} Mo libres — le prochain déploiement échouera à construire l'image"
  echo "     libérer : docker builder prune -af && docker image prune -af   (jamais --volumes)"
fi
free -m | sed -n "2p"
