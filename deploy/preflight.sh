#!/usr/bin/env bash
# Relevé AVANT migration, sur le VPS. Il répond à la seule question qui compte vraiment : y a-t-il des
# positions ouvertes sur l'ancien moteur ?
#
# Ce script distingue TROIS réponses, et c'est tout son intérêt :
#   • un nombre        → la lecture a abouti, on sait combien de positions sont ouvertes ;
#   • `inconnu`        → la lecture n'a PAS abouti, on ne sait pas ;
#   • aucune ligne     → le relevé lui-même a échoué (le workflow doit le traiter comme `inconnu`).
#
# Écrire 0 quand on n'a pas pu lire serait la pire erreur possible ici : « je n'ai pas réussi à
# savoir » deviendrait « il n'y a rien », juste avant l'effacement irréversible de la machine qui
# supervisait ces positions. Un moteur arrêté n'a pas fermé ses positions : elles restent ouvertes
# chez OKX, sans personne pour les surveiller.
#
# Sortie : lignes lisibles + une ligne finale `PREFLIGHT_POSITIONS=<n|inconnu>` et une ligne
# `PREFLIGHT_RAISON=<texte>` lues par le workflow.
set -u

positions="inconnu"
raison="non déterminé"

lire_positions() {
  if [ ! -f /root/hermes/.env ]; then
    raison="aucun /root/hermes/.env : ancien moteur absent, ou installé ailleurs"
    return
  fi
  if ! systemctl is-active hermes >/dev/null 2>&1; then
    # Cas explicitement dangereux : le service est arrêté, donc sa console ne répond pas, MAIS des
    # positions peuvent parfaitement être ouvertes chez OKX. L'inconnu est ici la bonne réponse.
    raison="service hermes inactif : sa console ne répond pas, or un moteur arrêté ne ferme rien"
    return
  fi
  TOK=$(grep "^HERMES_DASH_TOKEN=" /root/hermes/.env 2>/dev/null | tail -1 | cut -d= -f2-)
  if [ -z "${TOK:-}" ]; then
    raison="pas de HERMES_DASH_TOKEN : positions non lisibles depuis la console"
    return
  fi
  reponse=$(curl -s --max-time 8 -X POST "http://127.0.0.1:8899/api/fetch-portfolio?key=$TOK" \
    -H 'Content-Type: application/json' -d '{}' 2>/dev/null) || reponse=""
  if [ -z "$reponse" ]; then
    raison="console injoignable ou réponse vide (timeout 8 s)"
    return
  fi
  # `node` sort le nombre sur stdout et le détail sur stderr ; un JSON illisible sort `inconnu`.
  compte=$(printf '%s' "$reponse" | node -e '
    let b = "";
    process.stdin.on("data", (c) => (b += c));
    process.stdin.on("end", () => {
      try {
        const j = JSON.parse(b);
        const ps = (j.data && j.data.openPositionsDetails) || [];
        for (const p of ps) {
          console.error("  position ouverte : " + p.symbol + " " + p.side + " taille " + p.size + " pnl " + p.unrealizedPnl);
        }
        // Une réponse sans le champ attendu n’est pas une réponse vide : c’est une réponse qu’on ne
        // sait pas interpréter, donc `inconnu`.
        if (!j.data || !Array.isArray(j.data.openPositionsDetails)) {
          console.log("inconnu");
        } else {
          console.log(ps.length);
        }
      } catch {
        console.log("inconnu");
      }
    });' 2>&1)
  detail=$(printf '%s' "$compte" | sed '$d')
  [ -n "$detail" ] && printf '%s\n' "$detail"
  valeur=$(printf '%s' "$compte" | tail -1)
  case "$valeur" in
    ''|*[!0-9]*)
      raison="réponse de la console non interprétable"
      ;;
    *)
      positions="$valeur"
      raison="lu depuis la console de l'ancien moteur"
      ;;
  esac
}

echo "=== positions ouvertes sur l'ancien moteur ==="
lire_positions
echo "  résultat : $positions ($raison)"

if [ -f /root/hermes/.env ]; then
  if grep -q "^OKX_API_KEY=.\+" /root/hermes/.env 2>/dev/null; then
    echo "  clés OKX présentes dans l'ancien .env (elles ne seront PAS reprises)"
  else
    echo "  aucune clé OKX dans l'ancien .env"
  fi
fi

echo "=== services hermes ==="
systemctl list-units --no-legend 'hermes*' 2>/dev/null || true
echo "=== disque ==="
df -h / | tail -1
echo "=== docker ==="
if command -v docker >/dev/null 2>&1; then docker --version; else echo "  absent (sera installé)"; fi

echo "PREFLIGHT_POSITIONS=$positions"
echo "PREFLIGHT_RAISON=$raison"
