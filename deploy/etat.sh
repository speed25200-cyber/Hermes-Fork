#!/usr/bin/env bash
# État de la plateforme sur le VPS (poussé par `ssh ... bash -s`). N'imprime aucun secret.
set -u
DIR=/opt/okxq
PORT=8899

# Le profil compose découle du mode, et non d'une convention écrite ici : c'est lui qui décide quels
# services DOIVENT tourner, donc lui qui permet de repérer ceux qui tournent en trop.
MODE=$(grep -h "^OKXQ_MODE=" "$DIR/env/api.env" 2>/dev/null | tail -1 | cut -d= -f2-)
PROFIL=$(printf '%s' "${MODE:-PAPER}" | tr "[:upper:]" "[:lower:]")

echo "===== conteneurs ====="
cd "$DIR" 2>/dev/null && docker compose ps 2>/dev/null || echo "  /opt/okxq absent ou compose indisponible"
echo
echo "===== santé ====="
echo -n "  /health/live  : "; curl -s -o /dev/null -w "%{http_code}\n" --max-time 6 "http://127.0.0.1:${PORT}/health/live" || echo injoignable
echo -n "  /health/ready : "; curl -s -o /dev/null -w "%{http_code}\n" --max-time 6 "http://127.0.0.1:${PORT}/health/ready" || echo injoignable
echo -n "  / sans clé    : "; curl -s -o /dev/null -w "%{http_code}\n" --max-time 6 "http://127.0.0.1:${PORT}/" || echo injoignable
echo
echo "===== mode et clés (présence seulement) ====="
for f in api gateway jev-worker moteur; do
  if [ -f "$DIR/env/$f.env" ]; then
    echo "  $f.env : $(cut -d= -f1 "$DIR/env/$f.env" | tr '\n' ' ')"
  else
    echo "  $f.env : absent"
  fi
done
echo "  OKXQ_MODE=${MODE:-inconnu} (profil compose : $PROFIL)"
echo
echo "===== statut système (API locale, clé DÉRIVÉE du secret, jamais imprimée) ====="
# La clé d'accès n'est PAS le secret : c'est un HMAC du secret, par rôle. Ce script présentait le
# secret lui-même et recevait 403 — un diagnostic qui ressemblait à une panne d'authentification
# alors que c'était le script qui se trompait de valeur. Voir `okxq.api.auth.derive_role_keys`.
if [ -f "$DIR/env/api.env" ]; then
  SECRET=$(grep "^OPERATOR_AUTH_SECRET=" "$DIR/env/api.env" | tail -1 | cut -d= -f2-)
  CLE=$(printf 'okxq-ui-key:v1:admin' | openssl dgst -sha256 -hmac "$SECRET" -r | cut -d" " -f1)
  # La clé voyage par l'en-tête `Authorization`, JAMAIS par la chaîne de requête. Une URL traverse
  # le journal d'accès du serveur, les journaux de proxy, l'historique du navigateur et l'en-tête
  # Referer. C'est exactement ce qui s'est produit : la clé dérivée est apparue en clair dans le
  # journal d'uvicorn, puis dans la page publique du workflow qui recopiait ce journal.
  curl -s --max-time 8 -H "Authorization: Bearer $CLE" \
    "http://127.0.0.1:${PORT}/api/v1/system/status" | head -c 1500; echo
fi

echo
echo "===== journaux applicatifs (20 dernières lignes par service) ====="
# PostgreSQL est exclu VOLONTAIREMENT. Ses points de contrôle produisent une ligne toutes les cinq
# minutes, sans rapport avec la plateforme, et noyaient tout le reste : un `tail` sur ce rapport ne
# montrait plus que des « checkpoint complete ». Le diagnostic, lui, est en bas.
if [ -n "${DIR:-}" ] && cd "$DIR" 2>/dev/null; then
  SERVICES=$(docker compose ps --format '{{.Service}}' 2>/dev/null | grep -v "^postgres$" | sort -u | tr '\n' ' ')
  # shellcheck disable=SC2086
  [ -n "$SERVICES" ] && docker compose logs --tail=20 --no-color $SERVICES 2>/dev/null | tail -100 || true
fi

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

echo
echo "===== diagnostic (les questions qu'on se pose vraiment) ====="
# Ce bloc est en DERNIER, et c'est délibéré. Il était au milieu, avant les journaux : sur la page
# d'un run, et plus encore quand on n'en lit que la fin, il n'était jamais visible. Ce qui répond à
# « est-ce que ça marche ? » doit être la dernière chose écrite, pas la plus enfouie.
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

# Des conteneurs qu'un profil n'active plus continuent de tourner : `up` ne les arrête pas et
# `--remove-orphans` ne les voit pas comme orphelins, puisqu'ils figurent toujours dans le fichier
# compose. Au passage de PAPER aux cinq rôles séparés vers le moteur unique, les anciens sont restés
# en place — sur l'image précédente — et celui qui ne collectait rien réimposait `data_stale` à
# chaque frontière. Deux écrivains du même coupe-circuit, dont un aveugle : la plateforme restait
# bloquée alors que tous les conteneurs étaient « healthy ». On le dit donc ici, noir sur blanc.
ATTENDUS=$(cd "$DIR" 2>/dev/null && docker compose --profile "$PROFIL" config --services 2>/dev/null | sed "/^$/d" | sort -u)
PRESENTS=$(cd "$DIR" 2>/dev/null && docker compose ps --format '{{.Service}}' 2>/dev/null | sed "/^$/d" | sort -u)
if [ -z "$ATTENDUS" ]; then
  dire "conteneurs hors profil :" "indéterminé (liste des services illisible)"
else
  INTRUS=""
  for svc in $PRESENTS; do
    printf '%s\n' "$ATTENDUS" | grep -qx -- "$svc" || INTRUS="$INTRUS $svc"
  done
  if [ -n "$INTRUS" ]; then
    dire "conteneurs hors profil :" "!!$INTRUS — ils tournent EN TROP et écrivent le même état"
    echo "     relancer le déploiement les supprime ; à la main :"
    for svc in $INTRUS; do
      echo "       docker rm -f \"okxq-${svc}-1\""
    done
  else
    dire "conteneurs hors profil :" "aucun"
  fi
fi

DECIDEURS=$(printf '%s' "$JOURNAUX" | grep '"event": "decision"' | grep -o '"role": "[a-z-]*"' | sort -u | cut -d'"' -f4 | tr '\n' ' ')
dire "rôles qui décident :" "${DECIDEURS:-aucun (aucune frontière depuis 10 min)}"
HALT=$(printf '%s' "$JOURNAUX" | grep '"event": "halt_change"' | tail -1 | grep -o '"apres": "[A-Z_]*"' | cut -d'"' -f4)
dire "dernier changement de halt :" "${HALT:-aucun}"
DERNIERE=$(printf '%s' "$JOURNAUX" | grep '"event": "decision"' | tail -1 | grep -o '"reason_codes": "[^"]*"' | cut -d'"' -f4)
dire "motifs de la dernière décision :" "${DERNIERE:-—}"
