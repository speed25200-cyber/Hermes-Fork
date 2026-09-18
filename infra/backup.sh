#!/usr/bin/env bash
# Sauvegarde de la plateforme (§62) : base, manifests de données, configurations, modèles et
# approbations. Horodatée, vérifiée, chiffrée si une phrase de passe est disponible, à permissions
# restreintes sinon.
#
# POURQUOI CE SCRIPT EXISTE SOUS CETTE FORME :
#   • Le vidage est RELU (`pg_restore --list`) avant d'être conservé. Un fichier qui grossit n'est pas
#     une sauvegarde ; un fichier dont le sommaire se lit en est une présomption sérieuse.
#   • Quand le chiffrement est actif, l'archive est DÉCHIFFRÉE puis son flux tar est listé, sans jamais
#     être écrite en clair sur le disque. Cela prouve que la phrase de passe déposée rendra l'archive
#     exploitable — une archive chiffrée avec une phrase perdue est une perte de données.
#   • Les SECRETS NE SONT PAS SAUVEGARDÉS. env/*.env reste dehors : §62 demande base, manifests,
#     configurations, modèles et approbations, pas les clés. Elles se reposent par l'installateur ou
#     par la procédure de rotation (§60) ; les dupliquer dans des archives multiplierait les copies à
#     protéger.
#   • Aucune valeur secrète n'est imprimée, ni passée en argument de commande (elle serait visible dans
#     la liste des processus) : la phrase de passe transite par un fichier 600 dans un répertoire privé.
#
# CE QUE CE SCRIPT NE PROUVE PAS : « une sauvegarde non restaurée en test n'est pas une preuve de
# reprise » (§62). Seul infra/restore.sh exécuté sur une base jetable établit le RTO réel.
#
# Usage :
#   infra/backup.sh [--dir /opt/okxq] [--out /chemin/sauvegardes] [--keep 14] [--no-volumes]
# Variables :
#   OKXQ_BACKUP_PASSPHRASE   phrase de passe de chiffrement (sinon lue dans env/postgres.env|db.env)
#   OKXQ_BACKUP_DIR          répertoire de sortie par défaut
#   OKXQ_DB_SERVICE          nom du service compose de la base (défaut : postgres)
#   OKXQ_COMPOSE_PROJECT     nom du projet compose (défaut : okxq, fixé par `name:` dans compose.yaml)

set -euo pipefail
umask 077 # tout ce que ce script crée est illisible pour les autres, dès la création

# ── Emplacement du déploiement : le dossier parent de infra/, sauf indication contraire.
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
DIR=$(cd -- "$SCRIPT_DIR/.." && pwd)
OUT=${OKXQ_BACKUP_DIR:-}
KEEP=14
WITH_VOLUMES=1
DB_SERVICE=${OKXQ_DB_SERVICE:-postgres}
PROJECT=${OKXQ_COMPOSE_PROJECT:-okxq}
HELPER_IMAGE=${OKXQ_BACKUP_HELPER_IMAGE:-postgres:16-alpine}

while [ $# -gt 0 ]; do
  case "$1" in
    --dir) DIR=$2; shift 2 ;;
    --out) OUT=$2; shift 2 ;;
    --keep) KEEP=$2; shift 2 ;;
    --no-volumes) WITH_VOLUMES=0; shift ;;
    -h|--help) sed -n '1,40p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "argument inconnu : $1" >&2; exit 2 ;;
  esac
done

OUT=${OUT:-$DIR/backups}
ENVDIR="$DIR/env"
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
NAME="okxq-$STAMP"

echo "=== sauvegarde $NAME (UTC) ==="
echo "  déploiement : $DIR"
echo "  destination : $OUT"

command -v docker >/dev/null 2>&1 || { echo "  !! docker absent" >&2; exit 1; }
docker compose version >/dev/null 2>&1 || { echo "  !! docker compose absent" >&2; exit 1; }
[ -f "$DIR/compose.yaml" ] || { echo "  !! $DIR/compose.yaml introuvable" >&2; exit 1; }
cd "$DIR"

# ── Lecture des paramètres NON SECRETS de la base. On n'exécute pas le fichier (`source` exécuterait
#    du code arbitraire trouvé dans un fichier de secrets) : on extrait la valeur ligne par ligne.
lire_env() { # fichier clé
  [ -f "$1" ] || return 0
  grep -m1 "^$2=" "$1" 2>/dev/null | cut -d= -f2- || true
}
PGUSER=$(lire_env "$ENVDIR/postgres.env" POSTGRES_USER)
[ -n "$PGUSER" ] || PGUSER=$(lire_env "$ENVDIR/db.env" POSTGRES_USER)
PGUSER=${PGUSER:-okxq}
PGDB=$(lire_env "$ENVDIR/postgres.env" POSTGRES_DB)
[ -n "$PGDB" ] || PGDB=$(lire_env "$ENVDIR/db.env" POSTGRES_DB)
PGDB=${PGDB:-okxq_paper}

# ── Phrase de passe : variable d'environnement d'abord, fichier de service ensuite. Jamais imprimée.
PASSPHRASE=${OKXQ_BACKUP_PASSPHRASE:-}
if [ -z "$PASSPHRASE" ]; then
  PASSPHRASE=$(lire_env "$ENVDIR/postgres.env" OKXQ_BACKUP_PASSPHRASE)
fi
if [ -z "$PASSPHRASE" ]; then
  PASSPHRASE=$(lire_env "$ENVDIR/db.env" OKXQ_BACKUP_PASSPHRASE)
fi
case "$PASSPHRASE" in
  # Un modèle recopié tel quel n'est pas une phrase de passe : mieux vaut une archive en clair à
  # permissions restreintes, annoncée comme telle, qu'un chiffrement que l'on croit avoir.
  *remplacer-par*|*REMPLACER*) echo "  !! phrase de passe encore au modèle : ignorée" >&2; PASSPHRASE="" ;;
esac
if [ -n "$PASSPHRASE" ] && ! command -v openssl >/dev/null 2>&1; then
  echo "  !! openssl absent : chiffrement impossible" >&2
  PASSPHRASE=""
fi

mkdir -p "$OUT"; chmod 700 "$OUT"
# La zone de travail vit DANS le répertoire de sauvegarde : l'empaquetage final est alors un simple
# renommage sur le même système de fichiers, et un vidage volumineux ne remplit pas /tmp.
STAGE="$OUT/.$NAME.partiel"
WORK="$STAGE/$NAME"
rm -rf "$STAGE"; mkdir -p "$WORK"; chmod 700 "$STAGE" "$WORK"
# Répertoire privé réservé aux petits fichiers sensibles (phrase de passe, listings de vérification).
PRIV=$(mktemp -d); chmod 700 "$PRIV"
nettoyer() { rm -rf "$PRIV" "$STAGE"; }
trap nettoyer EXIT INT TERM

# ══ 1. Base de données ════════════════════════════════════════════════════════════════════════════
# Format personnalisé (-Fc) : compressé, restaurable sélectivement et lisible par `pg_restore --list`,
# ce qui rend la vérification d'intégrité possible sans restaurer.
# --no-owner / --no-privileges : la base cible peut appartenir à un autre rôle (bac à sable de test).
echo "--- 1. pg_dump ($PGDB) ---"
if ! docker compose ps --status running --services 2>/dev/null | grep -qx "$DB_SERVICE"; then
  echo "  !! service $DB_SERVICE non démarré : la base ne peut pas être vidée" >&2
  exit 1
fi
docker compose exec -T "$DB_SERVICE" \
  pg_dump -U "$PGUSER" -d "$PGDB" --format=custom --no-owner --no-privileges --compress=6 \
  > "$WORK/base.dump"
[ -s "$WORK/base.dump" ] || { echo "  !! vidage vide" >&2; exit 1; }
echo "  base.dump : $(wc -c < "$WORK/base.dump") octets"

echo "--- 2. vérification du vidage (relecture du sommaire) ---"
# pg_restore lit l'entrée standard quand aucun fichier n'est donné : le sommaire est produit par le
# MÊME major PostgreSQL que la restauration utilisera, ce qu'un pg_restore d'hôte ne garantit pas.
if ! docker compose exec -T "$DB_SERVICE" pg_restore --list > "$WORK/base.dump.toc" < "$WORK/base.dump"; then
  echo "  !! sommaire illisible : vidage corrompu, sauvegarde abandonnée" >&2
  exit 1
fi
echo "  sommaire : $(grep -cv '^;' "$WORK/base.dump.toc" || true) entrées restaurables"

# ══ 3. Configurations, approbations, manifests et modèles ═════════════════════════════════════════
echo "--- 3. configurations et artefacts ---"
# Fichiers d'infrastructure et profils validés : ils décrivent CE QUI tournait au moment du vidage.
# env/ est exclu explicitement (secrets, cf. en-tête).
# On NE masque PAS stderr : si tar se plaint, on veut lire pourquoi. Une sauvegarde qui échoue en
# silence est une sauvegarde qu'on croit avoir.
tar -czf "$WORK/config.tar.gz" -C "$DIR" \
  --exclude='env' --exclude='env/*' --exclude='backups' \
  configs compose.yaml infra || {
    echo "  !! archivage des configurations impossible" >&2; exit 1; }
echo "  config.tar.gz : configs/, compose.yaml, infra/ (env/ volontairement exclu)"

# Volumes nommés : manifests de données, modèles et rapports vivent dans des volumes Docker, pas sur
# l'hôte. On les lit avec un conteneur jetable SANS RÉSEAU, en lecture seule, en réutilisant une image
# déjà présente pour ne rien télécharger.
sauver_volume() { # nom_volume nom_fichier
  local vol="${PROJECT}_$1" fichier=$2
  if ! docker volume inspect "$vol" >/dev/null 2>&1; then
    echo "  volume $vol : absent (ignoré)"
    return 0
  fi
  docker run --rm --network none --user 0:0 \
    -v "$vol":/source:ro -v "$WORK":/sortie "$HELPER_IMAGE" \
    tar -czf "/sortie/$fichier" -C /source . >/dev/null 2>&1 \
    || { echo "  !! archivage du volume $vol impossible" >&2; return 1; }
  echo "  volume $vol -> $fichier ($(wc -c < "$WORK/$fichier") octets)"
}
if [ "$WITH_VOLUMES" = "1" ]; then
  sauver_volume okxq-artifacts artifacts.tar.gz # modèles et approbations signées (§56, §71)
  sauver_volume okxq-data data.tar.gz           # manifests de données et archive brute (§6)
  sauver_volume okxq-reports reports.tar.gz     # rapports de recherche et de réconciliation
else
  echo "  volumes ignorés (--no-volumes)"
fi

# ══ 4. Manifeste et empreintes ════════════════════════════════════════════════════════════════════
echo "--- 4. empreintes ---"
( cd "$WORK" && sha256sum -- * > SHA256SUMS && sha256sum -c --quiet SHA256SUMS ) \
  || { echo "  !! empreintes incohérentes" >&2; exit 1; }
echo "  SHA256SUMS vérifié"

CHIFFRE=non
[ -n "$PASSPHRASE" ] && CHIFFRE=aes-256-cbc-pbkdf2
COMMIT=$(git -C "$DIR" rev-parse --short HEAD 2>/dev/null || echo inconnu)
# Le manifeste reste EN CLAIR : c'est l'inventaire qui permet de choisir une archive sans la déchiffrer.
# Il ne contient aucun secret — ni mot de passe, ni phrase de passe, ni clé.
cat > "$WORK/manifeste.json" <<FIN
{
  "nom": "$NAME",
  "cree_a": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "fuseau": "UTC",
  "base": { "nom": "$PGDB", "utilisateur": "$PGUSER", "format": "pg_dump custom", "major": 16 },
  "chiffrement": "$CHIFFRE",
  "projet_compose": "$PROJECT",
  "commit": "$COMMIT",
  "contenu": ["base.dump", "base.dump.toc", "config.tar.gz", "artifacts.tar.gz", "data.tar.gz", "reports.tar.gz"],
  "secrets_inclus": false,
  "note": "Restauration : infra/restore.sh. Une sauvegarde non restaurée en test ne prouve aucune reprise (§62). Après restauration, réconcilier avec l'exchange avant toute reprise."
}
FIN
( cd "$WORK" && sha256sum manifeste.json >> SHA256SUMS )

# ══ 5. Empaquetage, chiffrement et vérification finale ═══════════════════════════════════════════
echo "--- 5. empaquetage ---"
# Les entrées de l'archive sont préfixées par le nom de la sauvegarde : une extraction ne peut pas
# éparpiller des fichiers dans le répertoire courant.
tar -cf "$STAGE/$NAME.tar" -C "$STAGE" "$NAME"
FINAL="$OUT/$NAME.tar"
if [ -n "$PASSPHRASE" ]; then
  PASSFILE="$PRIV/passe"
  printf '%s' "$PASSPHRASE" > "$PASSFILE"; chmod 600 "$PASSFILE"
  FINAL="$OUT/$NAME.tar.enc"
  # -pbkdf2 -iter 600000 : une phrase de passe humaine doit coûter cher à essayer hors ligne.
  # -pass file: évite que la phrase apparaisse dans la liste des processus.
  openssl enc -aes-256-cbc -pbkdf2 -iter 600000 -md sha512 -salt \
    -in "$STAGE/$NAME.tar" -out "$FINAL" -pass "file:$PASSFILE"
  echo "  chiffré : $(basename "$FINAL")"
  # Vérification réelle de la restituabilité : on déchiffre en flux et on liste le tar. Rien n'est
  # écrit en clair sur le disque. Si la phrase de passe est mauvaise, la sauvegarde échoue MAINTENANT,
  # pas le jour de l'incident.
  if ! openssl enc -d -aes-256-cbc -pbkdf2 -iter 600000 -md sha512 \
        -in "$FINAL" -pass "file:$PASSFILE" | tar -tf - > "$PRIV/liste" 2>/dev/null; then
    echo "  !! l'archive chiffrée ne se relit pas : sauvegarde abandonnée" >&2
    rm -f "$FINAL"
    exit 1
  fi
  echo "  relecture déchiffrée : $(wc -l < "$PRIV/liste") entrées"
else
  mv "$STAGE/$NAME.tar" "$FINAL"
  echo "  NON CHIFFRÉ : aucune phrase de passe disponible. Permissions restreintes (600) appliquées ;"
  echo "  poser OKXQ_BACKUP_PASSPHRASE dans env/postgres.env pour chiffrer (§62)."
  tar -tf "$FINAL" > "$PRIV/liste" || { echo "  !! archive illisible" >&2; exit 1; }
  echo "  relecture : $(wc -l < "$PRIV/liste") entrées"
fi
chmod 600 "$FINAL"
( cd "$OUT" && sha256sum "$(basename "$FINAL")" > "$(basename "$FINAL").sha256" )
chmod 600 "$FINAL.sha256"
cp "$WORK/manifeste.json" "$OUT/$NAME.manifeste.json"; chmod 600 "$OUT/$NAME.manifeste.json"
( cd "$OUT" && sha256sum -c --quiet "$(basename "$FINAL").sha256" ) \
  || { echo "  !! empreinte finale incohérente" >&2; exit 1; }
echo "  empreinte : $(cut -d' ' -f1 < "$FINAL.sha256")"

# ══ 6. Rétention ═════════════════════════════════════════════════════════════════════════════════
# Conserver KEEP archives. Le tri est lexicographique : l'horodatage ISO compact le rend chronologique.
if [ "$KEEP" -gt 0 ] 2>/dev/null; then
  mapfile -t anciennes < <(ls -1 "$OUT" 2>/dev/null | grep -E '^okxq-[0-9]{8}T[0-9]{6}Z\.tar(\.enc)?$' | sort)
  total=${#anciennes[@]}
  if [ "$total" -gt "$KEEP" ]; then
    a_supprimer=$((total - KEEP))
    for ((i = 0; i < a_supprimer; i++)); do
      base=${anciennes[$i]}
      rm -f "$OUT/$base" "$OUT/$base.sha256" "$OUT/${base%%.tar*}.manifeste.json"
      echo "  purge : $base"
    done
  fi
  echo "  rétention : $KEEP archives conservées"
fi

echo "=== sauvegarde OK : $FINAL ==="
echo "    Prochaine étape OBLIGATOIRE pour parler de reprise : restaurer cette archive sur une base"
echo "    jetable (infra/restore.sh) et mesurer le RTO. §62 : une sauvegarde non restaurée en test"
echo "    n'est pas une preuve de reprise."
