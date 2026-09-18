#!/usr/bin/env bash
# Supprime les conteneurs des services que le profil compose actif n'active PAS.
#
# Pourquoi ce script existe : un service qu'un profil n'active plus n'est pas arrêté pour autant.
# `docker compose up` ne touche pas aux services désactivés, et `--remove-orphans` ne les considère
# pas comme orphelins — ils figurent toujours dans le fichier compose. Changer les profils ne change
# donc RIEN à ce qui tourne déjà.
#
# Ce qu'il a coûté : au passage de PAPER des cinq rôles séparés au moteur unique, les cinq anciens
# conteneurs ont continué de tourner à côté du nouveau, sur l'image précédente. Deux processus
# écrivaient le même coupe-circuit ; celui qui ne collectait rien réimposait `data_stale` à chaque
# frontière et annulait le travail de l'autre. Sept conteneurs « healthy », aucun journal d'erreur,
# et une plateforme bloquée. Le déploiement se déclarait réussi.
#
# Usage : nettoyer_hors_profil.sh [profil]   (défaut : $OKXQ_PROFILE, sinon `paper`)
# À lancer depuis le répertoire du projet compose.
set -u

PROFILE=${1:-${OKXQ_PROFILE:-paper}}

# La liste des services à garder vient de compose lui-même, et jamais d'une liste écrite ici : deux
# listes finissent par diverger, et celle-ci déciderait de supprimer la base de données.
ACTIFS=$(docker compose --profile "$PROFILE" config --services 2>/dev/null | sed "/^$/d" | sort -u)
if [ -z "$ACTIFS" ]; then
  # Une liste vide ferait tout disparaître, PostgreSQL compris. On refuse plutôt que de deviner.
  echo "  !! liste des services du profil $PROFILE illisible — aucun conteneur supprimé"
  exit 1
fi

PRESENTS=$(docker compose ps -a --format '{{.Service}} {{.Name}}' 2>/dev/null)
SUPPRIMES=0
ECHECS=0
while read -r svc nom; do
  if [ -z "$svc" ] || [ -z "$nom" ]; then
    continue
  fi
  if printf '%s\n' "$ACTIFS" | grep -qx -- "$svc"; then
    continue
  fi
  echo "  $svc : hors du profil $PROFILE — suppression de $nom"
  if docker rm -f "$nom" >/dev/null 2>&1; then
    SUPPRIMES=$((SUPPRIMES + 1))
  else
    echo "    !! suppression de $nom impossible"
    ECHECS=$((ECHECS + 1))
  fi
done <<EOF
$PRESENTS
EOF

if [ "$SUPPRIMES" -eq 0 ] && [ "$ECHECS" -eq 0 ]; then
  echo "  aucun conteneur hors profil"
else
  echo "  $SUPPRIMES conteneur(s) hors profil supprimé(s), $ECHECS échec(s)"
fi
# Un échec de suppression laisse un écrivain de trop en place : c'est exactement la panne que ce
# script existe pour empêcher, et elle ne doit pas passer pour un succès.
[ "$ECHECS" -eq 0 ]
