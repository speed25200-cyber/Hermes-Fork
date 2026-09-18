#!/usr/bin/env bash
# Restauration d'une sauvegarde (§62). CETTE OPÉRATION ÉCRASE DES DONNÉES.
#
# POURQUOI UNE CONFIRMATION EXPLICITE EST EXIGÉE : `pg_restore --clean` supprime puis recrée les
# objets de la base cible. Sur la base d'exploitation, cela détruit le journal financier courant —
# ordres, fills, comptabilité, high-water mark. Aucun drapeau court, aucune réponse « o/n » : il faut
# taper le nom exact de la base visée, précédé du mot RESTAURER. Une faute de frappe doit interrompre
# l'opération, pas la lancer.
#
# CE QU'UNE RESTAURATION NE FAIT PAS (§62) : « une restauration du disque ne rétablit pas à elle seule
# la position réelle du compte ». Après restauration, la base décrit l'état d'AVANT la sauvegarde ;
# le compte chez OKX a pu bouger depuis. La réconciliation avec l'exchange est OBLIGATOIRE avant toute
# reprise, et ce script refuse de redémarrer les services à votre place.
#
# LIVE n'est jamais activé par ce script, et il n'existe aucune option pour le faire.
#
# Usage :
#   infra/restore.sh --file /opt/okxq/backups/okxq-<horodatage>.tar.enc [--db okxq_bac_a_sable]
#   infra/restore.sh --latest --db okxq_bac_a_sable        # essai de reprise sur une base jetable
#   infra/restore.sh --file ... --verify-only              # vérifie l'archive sans rien écraser
# Confirmation non interactive (automatisation d'un essai de reprise SEULEMENT) :
#   OKXQ_RESTORE_CONFIRM="RESTAURER <nom_de_la_base>" infra/restore.sh --file ... --db <nom_de_la_base>
# Variables :
#   OKXQ_BACKUP_PASSPHRASE   phrase de passe de déchiffrement (sinon lue dans env/postgres.env|db.env)
#   OKXQ_DB_SERVICE          service compose de la base (défaut : postgres)

set -euo pipefail
umask 077

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
DIR=$(cd -- "$SCRIPT_DIR/.." && pwd)
ARCHIVE=""
LATEST=0
TARGET_DB=""
VERIFY_ONLY=0
KEEP_SERVICES=0
DB_SERVICE=${OKXQ_DB_SERVICE:-postgres}
SERVICES_APP="collector strategy risk gateway jev-worker api"

while [ $# -gt 0 ]; do
  case "$1" in
    --dir) DIR=$2; shift 2 ;;
    --file) ARCHIVE=$2; shift 2 ;;
    --latest) LATEST=1; shift ;;
    --db) TARGET_DB=$2; shift 2 ;;
    --verify-only) VERIFY_ONLY=1; shift ;;
    --keep-services) KEEP_SERVICES=1; shift ;;
    -h|--help) sed -n '1,30p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "argument inconnu : $1" >&2; exit 2 ;;
  esac
done

ENVDIR="$DIR/env"
OUT=${OKXQ_BACKUP_DIR:-$DIR/backups}

# Docker n'est exigé que pour ÉCRIRE. Vérifier qu'une archive est exploitable (`--verify-only`) ne
# touche à aucune base et doit donc marcher partout : sur un poste, sur une machine de secours, dans
# la CI. Une sauvegarde qu'on ne peut contrôler qu'avec la pile complète est une sauvegarde qu'on
# contrôle moins souvent — et une sauvegarde jamais restaurée n'est pas une sauvegarde.
if [ "$VERIFY_ONLY" != "1" ]; then
  command -v docker >/dev/null 2>&1 || { echo "!! docker absent" >&2; exit 1; }
  docker compose version >/dev/null 2>&1 || { echo "!! docker compose absent" >&2; exit 1; }
  [ -f "$DIR/compose.yaml" ] || { echo "!! $DIR/compose.yaml introuvable" >&2; exit 1; }
fi
cd "$DIR"

if [ "$LATEST" = "1" ]; then
  ARCHIVE=$(ls -1 "$OUT"/okxq-*.tar.enc "$OUT"/okxq-*.tar 2>/dev/null | sort | tail -1 || true)
  [ -n "$ARCHIVE" ] || { echo "!! aucune archive dans $OUT" >&2; exit 1; }
fi
[ -n "$ARCHIVE" ] || { echo "!! --file ou --latest est requis" >&2; exit 2; }
[ -f "$ARCHIVE" ] || { echo "!! archive introuvable : $ARCHIVE" >&2; exit 1; }

lire_env() { # fichier clé — jamais `source` : un fichier de secrets ne doit pas être exécuté
  [ -f "$1" ] || return 0
  grep -m1 "^$2=" "$1" 2>/dev/null | cut -d= -f2- || true
}
PGUSER=$(lire_env "$ENVDIR/postgres.env" POSTGRES_USER)
[ -n "$PGUSER" ] || PGUSER=$(lire_env "$ENVDIR/db.env" POSTGRES_USER)
PGUSER=${PGUSER:-okxq}
if [ -z "$TARGET_DB" ]; then
  TARGET_DB=$(lire_env "$ENVDIR/postgres.env" POSTGRES_DB)
  [ -n "$TARGET_DB" ] || TARGET_DB=$(lire_env "$ENVDIR/db.env" POSTGRES_DB)
  TARGET_DB=${TARGET_DB:-okxq_paper}
fi

echo "=== restauration ==="
echo "  archive      : $ARCHIVE"
echo "  base cible   : $TARGET_DB (utilisateur $PGUSER)"
echo "  service base : $DB_SERVICE"

PRIV=$(mktemp -d); chmod 700 "$PRIV"
nettoyer() { rm -rf "$PRIV"; }
trap nettoyer EXIT INT TERM

# ══ 1. Intégrité de l'archive AVANT toute écriture ═══════════════════════════════════════════════
# On vérifie d'abord, on écrase ensuite. Restaurer une archive tronquée détruirait la base courante
# sans rien rétablir.
echo "--- 1. intégrité ---"
if [ -f "$ARCHIVE.sha256" ]; then
  ( cd "$(dirname "$ARCHIVE")" && sha256sum -c --quiet "$(basename "$ARCHIVE").sha256" ) \
    || { echo "  !! empreinte SHA-256 incohérente : archive altérée ou incomplète" >&2; exit 1; }
  echo "  empreinte du fichier : OK"
else
  echo "  !! pas de fichier .sha256 à côté de l'archive : intégrité NON VÉRIFIÉE" >&2
fi

case "$ARCHIVE" in
  *.enc)
    PASSPHRASE=${OKXQ_BACKUP_PASSPHRASE:-}
    if [ -z "$PASSPHRASE" ]; then PASSPHRASE=$(lire_env "$ENVDIR/postgres.env" OKXQ_BACKUP_PASSPHRASE); fi
    if [ -z "$PASSPHRASE" ]; then PASSPHRASE=$(lire_env "$ENVDIR/db.env" OKXQ_BACKUP_PASSPHRASE); fi
    [ -n "$PASSPHRASE" ] || { echo "  !! archive chiffrée et aucune phrase de passe disponible" >&2; exit 1; }
    PASSFILE="$PRIV/passe"; printf '%s' "$PASSPHRASE" > "$PASSFILE"; chmod 600 "$PASSFILE"
    # Déchiffrement vers le répertoire privé (700). La phrase de passe passe par un fichier, jamais en
    # argument de commande.
    openssl enc -d -aes-256-cbc -pbkdf2 -iter 600000 -md sha512 \
      -in "$ARCHIVE" -out "$PRIV/bundle.tar" -pass "file:$PASSFILE" \
      || { echo "  !! déchiffrement impossible (phrase de passe ?)" >&2; exit 1; }
    echo "  déchiffrement : OK"
    ;;
  *) cp "$ARCHIVE" "$PRIV/bundle.tar" ;;
esac

tar -xf "$PRIV/bundle.tar" -C "$PRIV"
BUNDLE=$(find "$PRIV" -mindepth 1 -maxdepth 1 -type d -name 'okxq-*' | head -1)
[ -n "$BUNDLE" ] || { echo "  !! contenu d'archive inattendu" >&2; exit 1; }
( cd "$BUNDLE" && sha256sum -c --quiet SHA256SUMS ) \
  || { echo "  !! empreintes internes incohérentes" >&2; exit 1; }
echo "  empreintes internes : OK"
[ -s "$BUNDLE/base.dump" ] || { echo "  !! base.dump absent ou vide" >&2; exit 1; }
if [ -f "$BUNDLE/manifeste.json" ]; then
  echo "--- manifeste ---"
  sed 's/^/  /' "$BUNDLE/manifeste.json"
fi

if [ "$VERIFY_ONLY" = "1" ]; then
  echo "=== vérification seule : archive exploitable, RIEN n'a été écrasé ==="
  exit 0
fi

# ══ 2. Confirmation explicite ════════════════════════════════════════════════════════════════════
ATTENDU="RESTAURER $TARGET_DB"
echo "--- 2. confirmation ---"
echo "  ATTENTION : la base « $TARGET_DB » va être NETTOYÉE puis remplacée par le contenu de l'archive."
echo "  Tout ce qui y a été écrit depuis $(basename "$ARCHIVE") sera PERDU :"
echo "    ordres, fills, comptabilité, événements de risque, high-water mark, approbations."
echo "  Pour confirmer, tapez exactement : $ATTENDU"
REPONSE=${OKXQ_RESTORE_CONFIRM:-}
if [ -n "$REPONSE" ]; then
  echo "  (confirmation fournie par OKXQ_RESTORE_CONFIRM)"
elif [ -t 0 ]; then
  printf '  > '
  IFS= read -r REPONSE
else
  echo "  !! entrée non interactive et OKXQ_RESTORE_CONFIRM absent : restauration refusée" >&2
  exit 1
fi
if [ "$REPONSE" != "$ATTENDU" ]; then
  echo "  !! confirmation incorrecte : restauration ABANDONNÉE, rien n'a été modifié" >&2
  exit 1
fi
echo "  confirmation acceptée"

# ══ 3. Arrêt des services applicatifs ════════════════════════════════════════════════════════════
# Restaurer sous une application vivante produirait un mélange de deux états : le gateway pourrait
# écrire un fill dans une base à demi restaurée. On arrête tout sauf la base elle-même.
echo "--- 3. arrêt des services applicatifs ---"
if [ "$KEEP_SERVICES" = "1" ]; then
  echo "  !! --keep-services : les services restent actifs (essai sur base jetable uniquement)" >&2
else
  # shellcheck disable=SC2086
  docker compose stop $SERVICES_APP 2>&1 | sed 's/^/  /' || true
fi

if ! docker compose ps --status running --services 2>/dev/null | grep -qx "$DB_SERVICE"; then
  echo "  démarrage du service $DB_SERVICE"
  docker compose up -d "$DB_SERVICE" 2>&1 | sed 's/^/  /'
  for _ in $(seq 1 30); do
    if docker compose exec -T "$DB_SERVICE" pg_isready -q -U "$PGUSER" >/dev/null 2>&1; then break; fi
    sleep 2
  done
fi

# ══ 4. Restauration ══════════════════════════════════════════════════════════════════════════════
echo "--- 4. pg_restore ---"
# La base cible peut ne pas exister (essai de reprise sur une base jetable) : on la crée si besoin.
docker compose exec -T "$DB_SERVICE" \
  createdb -U "$PGUSER" "$TARGET_DB" >/dev/null 2>&1 \
  && echo "  base $TARGET_DB créée" || echo "  base $TARGET_DB déjà présente"

# --clean --if-exists : les objets existants sont supprimés avant réinsertion. --single-transaction :
# soit tout est restauré, soit rien — on ne veut pas d'une base à moitié rétablie.
# --no-owner / --no-privileges : le rôle propriétaire de l'origine peut ne pas exister ici.
if docker compose exec -T "$DB_SERVICE" \
     pg_restore -U "$PGUSER" -d "$TARGET_DB" --clean --if-exists --no-owner --no-privileges \
     --single-transaction --exit-on-error < "$BUNDLE/base.dump"; then
  echo "  pg_restore : OK"
else
  echo "  !! pg_restore a échoué — la transaction unique garantit qu'aucune restauration partielle" >&2
  echo "     n'a été appliquée. La base cible est restée dans son état antérieur." >&2
  exit 1
fi

TABLES=$(docker compose exec -T "$DB_SERVICE" \
  psql -U "$PGUSER" -d "$TARGET_DB" -tAc \
  "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'" 2>/dev/null | tr -d '\r' || echo 0)
echo "  contrôle : $TABLES tables dans le schéma public"
[ "${TABLES:-0}" -gt 0 ] || { echo "  !! base restaurée vide : restauration considérée comme ÉCHOUÉE" >&2; exit 1; }

# ══ 5. Ce qui reste à faire — à la main, et volontairement ════════════════════════════════════════
cat <<'FIN'
=== restauration de la base : OK ===

Les services applicatifs restent ARRÊTÉS. Ce n'est pas un oubli (§62).

AVANT toute reprise :
  1. RÉCONCILIER avec l'exchange. La base décrit l'état d'avant la sauvegarde ; le compte réel a pu
     bouger depuis. Une restauration de disque ne rétablit pas la position réelle du compte.
     Il n'existe PAS de réconciliation à la demande : elle est déclenchée par la séquence de
     démarrage du gateway (étape `bootstrap_reconcile`), et tout ordre envoyé sans résultat persisté
     y devient UNKNOWN avant qu'aucune entrée ne soit autorisée. On l'observe ensuite :
       okxq risk status --config configs/paper.yaml
       okxq reports risk --config configs/paper.yaml
       curl -s -H "Authorization: Bearer <cle>" http://127.0.0.1:8899/api/v1/system/status
       curl -s -H "Authorization: Bearer <cle>" http://127.0.0.1:8899/api/v1/orders?state=UNKNOWN
     Tant que des ordres restent UNKNOWN, `/health/ready` répond 503 : c'est le bon comportement.
  1bis. REPOSER LE HALT s'il y en avait un après la sauvegarde. Une base restaurée peut RESSUSCITER
     une autorisation d'entrer en effaçant un halt postérieur à l'instantané. À faire AVANT de
     redémarrer les écrivains :
       okxq control pause --config configs/paper.yaml --reason "post-restauration" --actor <vous>
  2. Vérifier le high-water mark et la perte journalière : leurs seuils ont une frontière UTC déclarée
     et un redémarrage ne remet jamais les pertes à zéro (§38).
  3. Contrôler les ordres ouverts et les protections chez OKX avant de réactiver le gateway.
  4. Consigner le RTO mesuré (du début de cette restauration à la reprise effective) : c'est la seule
     valeur de reprise qui compte, celle qui a été mesurée.

Reprise, une fois la réconciliation faite :
       docker compose up -d

LIVE reste désactivé. Ce script ne l'active pas et n'offre aucun contournement (§71).

Les configurations, modèles, manifests et rapports contenus dans l'archive (config.tar.gz,
artifacts.tar.gz, data.tar.gz, reports.tar.gz) ne sont PAS déployés automatiquement : les écraser
pourrait remplacer un modèle promu par un modèle plus ancien. Les extraire explicitement si besoin.
FIN
