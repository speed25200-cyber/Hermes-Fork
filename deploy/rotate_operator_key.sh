#!/usr/bin/env bash
# Renouvelle la clé opérateur SUR LE SERVEUR, et VÉRIFIE que la nouvelle est acceptée.
#
# Toutes les clés d'accès (reader, operator, admin) sont dérivées de OPERATOR_AUTH_SECRET par HMAC :
# changer ce secret les révoque toutes d'un coup. À faire dès qu'une clé a pu être vue — un journal
# recopié, une capture d'écran, un lien partagé.
#
# La nouvelle valeur n'est jamais imprimée : ce script est exécuté depuis un workflow dont le journal
# est public. Son propriétaire la relit sur la machine.
#
# POURQUOI CE FICHIER EXISTE, et n'est pas un bloc écrit dans le workflow. Il l'était, à l'intérieur
# d'une chaîne entre guillemets simples passée à ssh. Les commentaires français contiennent des
# apostrophes — « l'échec », « c'est » — et une apostrophe FERME cette chaîne. Tout ce qui suivait
# s'exécutait alors sur le runner GitHub au lieu du serveur : la moitié du script tournait au mauvais
# endroit, en silence, et le message d'erreur parlait d'un répertoire absent. Un fichier envoyé par
# l'entrée standard (`ssh ... bash -s < ce-fichier`) n'a aucun problème de guillemets.
set -u

F=/opt/okxq/env/api.env
PORT=8899
if [ ! -f "$F" ]; then
  echo "::error::$F absent : la plateforme n est pas installée"
  exit 1
fi

NEUF=$(openssl rand -hex 32)
sed -i "/^OPERATOR_AUTH_SECRET=/d" "$F"
printf 'OPERATOR_AUTH_SECRET=%s\n' "$NEUF" >> "$F"
chmod 600 "$F"

# `--no-deps` est essentiel : sans lui, recréer un service recrée aussi ses dépendances — donc
# PostgreSQL et le conteneur de migration, qui rejoue les migrations. L API resterait indisponible
# plus d une minute et la vérification conclurait à tort. Seul le service dont le fichier
# d environnement a changé doit être recréé ; ses dépendances tournent déjà et n ont rien à relire.
#
# `restart` ne conviendrait pas : il réutilise le conteneur, et un fichier d environnement n est lu
# qu à la CRÉATION. Le fichier changerait, le conteneur garderait l ancienne valeur, et la clé
# révoquée resterait valide. Une révocation qui ne révoque rien est pire que pas de révocation : on
# se croit protégé.
cd /opt/okxq || { echo "::error::/opt/okxq introuvable"; exit 1; }
docker compose up -d --force-recreate --no-deps api || {
  echo "::error::recréation du service api impossible"
  exit 1
}

for _ in $(seq 1 45); do
  curl -s -o /dev/null --max-time 3 "http://127.0.0.1:${PORT}/health/live" && break
  sleep 2
done

NOUVELLE=$(printf 'okxq-ui-key:v1:admin' | openssl dgst -sha256 -hmac "$NEUF" -r | cut -d' ' -f1)
CODE=$(curl -s -o /dev/null -w '%{http_code}' --max-time 6 \
  -H "Authorization: Bearer $NOUVELLE" "http://127.0.0.1:${PORT}/api/v1/system/status" || echo 000)
if [ "$CODE" != "200" ]; then
  echo "::error::la NOUVELLE clé est refusée ($CODE) : le service n a pas relu son environnement, donc l ancienne clé reste valide"
  docker compose ps
  exit 1
fi

echo "  nouvelle clé acceptée (200) : les clés dérivées précédentes sont bien révoquées"
echo "  la lire, sur CETTE machine :"
echo '      S=$(grep ^OPERATOR_AUTH_SECRET= /opt/okxq/env/api.env | cut -d= -f2-)'
echo '      printf okxq-ui-key:v1:admin | openssl dgst -sha256 -hmac "$S" -r | cut -d" " -f1'
