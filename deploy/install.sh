#!/usr/bin/env bash
# Installateur okx-quant-jev pour le VPS — idempotent, à lancer en root.
# Appelé à distance par .github/workflows/deploy-vps.yml après rsync du dépôt vers /opt/okxq.
#
# Ce qu'il fait, dans l'ordre :
#   1. Docker + compose (si absents)          4. construction de l'image
#   2. réseau (ufw : SSH + 8899)               5. démarrage des services (profil PAPER)
#   3. secrets par service (jamais imprimés)   6. vérification : /health/live puis /health/ready
#
# Ce qu'il ne fait JAMAIS : poser des clés OKX en clair dans un env partagé, activer LIVE, imprimer un
# secret. Les clés OKX ne vont que dans env/gateway.env, la clé TypeSafe que dans env/jev-worker.env,
# la clé opérateur que dans env/api.env.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

DIR=${OKXQ_DIR:-/opt/okxq}
ENVDIR="$DIR/env"
PROFILE=${OKXQ_PROFILE:-paper}
PORT=${OKXQ_PORT:-8899}

echo "=== 1. Docker ==="
if ! command -v docker >/dev/null 2>&1; then
  apt-get update -qq
  apt-get -y -qq install ca-certificates curl gnupg
  curl -fsSL https://get.docker.com -o /tmp/get-docker.sh
  sh /tmp/get-docker.sh >/dev/null 2>&1
  rm -f /tmp/get-docker.sh
fi
docker --version
docker compose version >/dev/null 2>&1 || { echo "  !! docker compose absent"; exit 1; }
systemctl enable --now docker >/dev/null 2>&1 || true

echo "=== 2. réseau ==="
apt-get -y -qq install ufw >/dev/null 2>&1 || true
ufw allow OpenSSH >/dev/null 2>&1 || true
ufw allow "${PORT}/tcp" >/dev/null 2>&1 || true
ufw --force enable >/dev/null 2>&1 || true
echo "  ${PORT} ouvert ; PostgreSQL n'est pas exposé (réseau compose interne)"

echo "=== 3. secrets par service ==="
mkdir -p "$ENVDIR"; chmod 700 "$ENVDIR"
set +x
# Les valeurs arrivent par l'ENVIRONNEMENT (posées par le workflow depuis l'entrée standard), jamais en
# argument, jamais imprimées.
poser() { # fichier clé valeur
  local f="$ENVDIR/$1" k="$2" v="$3"
  touch "$f"; chmod 600 "$f"
  if [ -n "$v" ]; then
    sed -i "/^${k}=/d" "$f"; echo "${k}=${v}" >> "$f"; echo "  $1 : $k posé"
  elif grep -q "^${k}=.\+" "$f" 2>/dev/null; then
    echo "  $1 : $k conservé"
  else
    echo "  $1 : $k ABSENT"
  fi
}
# Clé opérateur : fournie (OKXQ_OPERATOR_KEY_POSE) > existante > générée.
if [ -z "${OKXQ_OPERATOR_KEY_POSE:-}" ] && ! grep -q "^OPERATOR_AUTH_SECRET=.\+" "$ENVDIR/api.env" 2>/dev/null; then
  OKXQ_OPERATOR_KEY_POSE=$(openssl rand -hex 16)
  echo "  clé opérateur GÉNÉRÉE (jamais imprimée)."
  echo "  Pour la lire, depuis CETTE machine :"
  echo "      grep OPERATOR_AUTH_SECRET /opt/okxq/env/api.env"
  echo "  Pour en poser une connue à la place : secret de dépôt OKXQ_OPERATOR_KEY, puis relancer."
fi
poser api.env OPERATOR_AUTH_SECRET "${OKXQ_OPERATOR_KEY_POSE:-}"
# Mot de passe PostgreSQL interne : généré une fois, partagé entre postgres et les services qui lisent la base.
if ! grep -q "^POSTGRES_PASSWORD=.\+" "$ENVDIR/postgres.env" 2>/dev/null; then
  poser postgres.env POSTGRES_PASSWORD "$(openssl rand -hex 24)"
fi
PGPW=$(grep "^POSTGRES_PASSWORD=" "$ENVDIR/postgres.env" | cut -d= -f2-)
poser postgres.env POSTGRES_USER okxq
poser postgres.env POSTGRES_DB "okxq_${PROFILE}"
DBURL="postgresql+psycopg://okxq:${PGPW}@postgres:5432/okxq_${PROFILE}"
for svc in api collector strategy risk gateway jev-worker migrate; do
  poser "$svc.env" DATABASE_URL "$DBURL"
  poser "$svc.env" OKXQ_MODE "$(echo "$PROFILE" | tr '[:lower:]' '[:upper:]')"
done
# `migrate.env` est posé explicitement : sans lui, le service de migration retomberait sur
# env/collector.env. Cela fonctionnerait, mais un fichier dédié permet d'y mettre plus tard un rôle
# PostgreSQL aux droits DDL minimaux sans toucher au collecteur (§60). Il ne porte aucun secret de
# fournisseur : migrer un schéma n'exige aucune clé d'échange.
poser gateway.env OKX_API_KEY "${OKX_API_KEY_POSE:-}"
poser gateway.env OKX_API_SECRET "${OKX_API_SECRET_POSE:-}"
poser gateway.env OKX_API_PASSPHRASE "${OKX_API_PASSPHRASE_POSE:-}"
poser gateway.env OKX_ACCOUNT_REGION_PROFILE "${OKX_ACCOUNT_REGION_PROFILE_POSE:-}"
poser jev-worker.env TYPESAFE_API_KEY "${TYPESAFE_API_KEY_POSE:-}"
set -x 2>/dev/null || true
set +x

echo "=== 4. image ==="
cd "$DIR"
docker compose --profile "$PROFILE" build --quiet 2>&1 | tail -3 || docker compose --profile "$PROFILE" build 2>&1 | tail -20

echo "=== 5. services (profil $PROFILE) ==="
docker compose --profile "$PROFILE" up -d --remove-orphans 2>&1 | tail -10

echo "=== 6. schéma de base ==="
# La migration est un travail à usage unique dont l'échec doit être VISIBLE. Sans ce contrôle, un
# schéma non appliqué laisserait les écrivains bloqués (ils attendent la fin de `migrate`) tandis que
# l'API, qui ne l'attend pas, resterait joignable : l'opérateur verrait une console vivante devant un
# système mort. C'est exactement le genre de panne qu'on met des heures à diagnostiquer.
mig_state=$(docker compose --profile "$PROFILE" ps -a --format '{{.Service}} {{.State}} {{.ExitCode}}' 2>/dev/null | awk '$1=="migrate"{print $2" "$3}' | tail -1)
echo "  migrate : ${mig_state:-état inconnu}"
case "$mig_state" in
  "exited 0")
    echo "  schéma appliqué"
    ;;
  "exited "*)
    echo "  !! la migration a échoué : les rôles écrivains ne démarreront pas"
    docker compose --profile "$PROFILE" logs --tail=40 migrate
    exit 1
    ;;
  *)
    # Ni terminé ni en échec : on ne conclut pas. Un état indéterminé n'est pas un succès.
    echo "  !! état de la migration non déterminé — vérifier avant d'utiliser la plateforme"
    docker compose --profile "$PROFILE" logs --tail=20 migrate || true
    ;;
esac

echo "=== 7. vérification ==="
code=000
for _ in $(seq 1 45); do
  code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 4 "http://127.0.0.1:${PORT}/health/live" || echo 000)
  [ "$code" = "200" ] && break
  sleep 2
done
if [ "$code" != "200" ]; then
  echo "  !! /health/live a répondu $code"; docker compose --profile "$PROFILE" ps; docker compose --profile "$PROFILE" logs --tail=40 api; exit 1
fi
echo "  /health/live : 200"
ready=$(curl -s -o /dev/null -w "%{http_code}" --max-time 4 "http://127.0.0.1:${PORT}/health/ready" || echo 000)
echo "  /health/ready : $ready (503 tant que la réconciliation/les données ne sont pas prêtes — normal au premier démarrage)"
# Ce que la porte DOIT répondre dépend d'un réglage, pas d'une conviction. Le contrôle lisait 403 en
# dur ; le jour où l'exploitant a ouvert l'accès en lecture, l'installation a échoué sur sa propre
# vérification alors que la plateforme marchait. Un contrôle qui ignore la configuration qu'il est
# censé vérifier finit par empêcher le comportement demandé.
#
# On lit donc l'attendu dans la configuration effective, et on vérifie l'égalité — pas une valeur
# choisie d'avance. Absent du fichier, le défaut est `non` : porte fermée.
ACCES=$(grep -E "^[[:space:]]*acces_sans_cle:" "/app/configs/${PROFILE}.yaml" 2>/dev/null \
  | tail -1 | sed "s/.*acces_sans_cle:[[:space:]]*//" | tr -d "\"' " )
ACCES=${ACCES:-non}
root=$(curl -s -o /dev/null -w "%{http_code}" --max-time 4 "http://127.0.0.1:${PORT}/" || echo 000)
case "$ACCES" in
  non)
    if [ "$root" = "403" ] || [ "$root" = "401" ]; then
      echo "  / sans clé : $root — la porte est fermée, c'est le bon comportement"
    else
      echo "  !! acces_sans_cle=non mais / sans clé a répondu $root : la porte devrait être fermée"
      exit 1
    fi
    ;;
  lecture|total)
    if [ "$root" = "200" ]; then
      echo "  / sans clé : 200 — PORTE OUVERTE (acces_sans_cle=$ACCES), demandé explicitement"
      echo "     toute personne qui atteint ce port voit le tableau de bord"
    else
      echo "  !! acces_sans_cle=$ACCES mais / sans clé a répondu $root : la porte devrait être ouverte"
      exit 1
    fi
    ;;
  *)
    echo "  !! acces_sans_cle inconnu dans la configuration : $ACCES"; exit 1
    ;;
esac
docker compose --profile "$PROFILE" ps
echo "install: OK — mode $(echo "$PROFILE" | tr '[:lower:]' '[:upper:]'), LIVE désactivé"
