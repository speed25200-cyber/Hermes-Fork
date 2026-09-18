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
echo "===== statut système (API locale, clé DÉRIVÉE du secret, jamais imprimée) ====="
# La clé d'accès n'est PAS le secret : c'est un HMAC du secret, par rôle. Ce script présentait le
# secret lui-même et recevait 403 — un diagnostic qui ressemblait à une panne d'authentification
# alors que c'était le script qui se trompait de valeur. Voir `okxq.api.auth.derive_role_keys`.
if [ -f "$DIR/env/api.env" ]; then
  SECRET=$(grep "^OPERATOR_AUTH_SECRET=" "$DIR/env/api.env" | tail -1 | cut -d= -f2-)
  CLE=$(printf 'okxq-ui-key:v1:admin' | openssl dgst -sha256 -hmac "$SECRET" -r | cut -d" " -f1)
  curl -s --max-time 8 "http://127.0.0.1:${PORT}/api/v1/system/status?key=$CLE" | head -c 1500; echo
fi

echo
echo "===== diagnostic (les questions qu'on se pose vraiment) ====="
# Un dépôt de journaux ne dit pas si la plateforme FONCTIONNE. Ces quatre lignes, si.
JOURNAUX=$(cd "$DIR" 2>/dev/null && docker compose logs --no-color --since 10m 2>/dev/null)
dire() { printf '  %-34s %s\n' "$1" "$2"; }
if printf '%s' "$JOURNAUX" | grep -q "manifeste de capacités introuvable"; then
  dire "manifeste de capacités :" "INTROUVABLE — aucune découverte d'univers possible"
else
  dire "manifeste de capacités :" "chargé"
fi
if printf '%s' "$JOURNAUX" | grep -q "Read-only file system"; then
  dire "écriture des alertes :" "REFUSÉE (système de fichiers en lecture seule)"
else
  dire "écriture des alertes :" "possible"
fi
DECIDEURS=$(printf '%s' "$JOURNAUX" | grep '"event": "decision"' | grep -o '"role": "[a-z-]*"' | sort -u | cut -d'"' -f4 | tr '\n' ' ')
dire "rôles qui décident :" "${DECIDEURS:-aucun (aucune frontière depuis 10 min)}"
HALT=$(printf '%s' "$JOURNAUX" | grep '"event": "halt_change"' | tail -1 | grep -o '"apres": "[A-Z_]*"' | cut -d'"' -f4)
dire "dernier changement de halt :" "${HALT:-aucun}"
DERNIERE=$(printf '%s' "$JOURNAUX" | grep '"event": "decision"' | tail -1 | grep -o '"reason_codes": "[^"]*"' | cut -d'"' -f4)
dire "motifs de la dernière décision :" "${DERNIERE:-—}"
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
