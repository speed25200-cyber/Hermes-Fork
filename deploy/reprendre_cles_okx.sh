#!/usr/bin/env bash
# Reprend les identifiants OKX de l'ANCIEN Hermes (/root/hermes/.env) et les pose dans le fichier
# d'environnement du SEUL service qui a le droit de les voir : env/gateway.env.
#
# Les valeurs ne quittent JAMAIS la machine. Elles ne transitent ni par GitHub, ni par le journal du
# workflow (qui est public), ni par une conversation. Ce script ne les imprime pas : il n'affiche que
# la PRÉSENCE de chaque variable.
#
# Ce que ce script ne fait PAS, et c'est important : il n'active rien. En PAPER, la plateforme
# n'ouvre aucune connexion privée — `build_exchange_adapter` rend un `VirtualExchange` et ne lit même
# pas ces variables. Poser les clés est une PRÉPARATION pour DEMO ; le passage effectif exige de
# changer de mode, et LIVE exige en plus un manifeste d'approbation signé.
set -u

ANCIEN=/root/hermes/.env
CIBLE=/opt/okxq/env/gateway.env

if [ ! -f "$ANCIEN" ]; then
  echo "::error::$ANCIEN absent : aucun ancien moteur pour reprendre les clés"
  exit 1
fi

lire() {
  # Dernière affectation gagnante, guillemets retirés. La valeur n'est jamais affichée.
  grep -E "^[[:space:]]*$1=" "$ANCIEN" 2>/dev/null | tail -1 | cut -d= -f2- | sed "s/^['\"]//;s/['\"]$//"
}

CLE=$(lire OKX_API_KEY)
SECRET=$(lire OKX_API_SECRET)
# L'ancien moteur nomme la phrase de passe `OKX_PASSPHRASE` ; la nouvelle plateforme attend
# `OKX_API_PASSPHRASE`. Recopier sans renommer aurait produit une authentification qui échoue avec un
# message d'OKX peu parlant, et on aurait cherché du côté de la signature.
PASSPHRASE=$(lire OKX_PASSPHRASE)
[ -z "$PASSPHRASE" ] && PASSPHRASE=$(lire OKX_API_PASSPHRASE)

if [ -z "$CLE" ] || [ -z "$SECRET" ] || [ -z "$PASSPHRASE" ]; then
  echo "::error::identifiants incomplets dans $ANCIEN (clé, secret et passphrase sont tous requis)"
  echo "  présence : cle=$([ -n "$CLE" ] && echo oui || echo non) secret=$([ -n "$SECRET" ] && echo oui || echo non) passphrase=$([ -n "$PASSPHRASE" ] && echo oui || echo non)"
  exit 1
fi

# Profil de région déduit du domaine que l'ancien moteur utilisait. Sans lui, DEMO et LIVE refusent de
# démarrer — et le refus arriverait bien plus tard, au premier essai de connexion.
BASE=$(lire OKX_BASE_URL)
case "$BASE" in
  *eea.okx.com*) REGION=eea ;;
  *app.okx.com*) REGION=us ;;
  *)             REGION=global ;;
esac

mkdir -p "$(dirname "$CIBLE")"
chmod 700 "$(dirname "$CIBLE")"
touch "$CIBLE"
chmod 600 "$CIBLE"
for kv in "OKX_API_KEY=$CLE" "OKX_API_SECRET=$SECRET" "OKX_API_PASSPHRASE=$PASSPHRASE" \
          "OKX_ACCOUNT_REGION_PROFILE=$REGION"; do
  k=${kv%%=*}
  sed -i "/^$k=/d" "$CIBLE"
  printf '%s\n' "$kv" >> "$CIBLE"
done

echo "  identifiants OKX repris depuis $ANCIEN vers $CIBLE (0600)"
echo "  variables posées : $(cut -d= -f1 "$CIBLE" | tr '\n' ' ')"
echo "  profil de région déduit : $REGION"
echo
echo "  ATTENTION : cela n active PAS le trading. Le mode reste celui de la configuration."
echo "  En PAPER, ces variables ne sont meme pas lues : l echange est simule localement."
echo "  DEMO exige de changer de mode ; LIVE exige en plus un manifeste d approbation signe."

# Recréer, et non redémarrer : un env_file n est lu qu a la CRÉATION du conteneur. `--no-deps` pour
# ne pas entraîner PostgreSQL et le conteneur de migration.
cd /opt/okxq || { echo "::error::/opt/okxq introuvable"; exit 1; }
docker compose up -d --force-recreate --no-deps gateway || {
  echo "::error::recréation du service gateway impossible"
  exit 1
}
echo "  gateway recree avec son nouvel environnement"
