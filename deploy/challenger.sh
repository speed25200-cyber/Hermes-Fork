#!/usr/bin/env bash
# Champion / challenger : installe le modèle <dossier> comme champion selon une règle unique, partagée par le
# réentraînement mensuel sur runner GitHub (workflow Retrain) et par retrain.sh sur un VPS d'au moins 12 Go.
# Même configuration que le champion : la nouvelle évaluation (plus de données) fait foi et le remplace
# toujours -- un champion promu qui échoue désormais la porte est rétrogradé (le moteur réel aplatit alors le
# livre) ; une réussite chanceuse ne devient jamais permanente.
# Configuration différente : le challenger ne remplace le champion que s'il est promu, ou si le champion ne
# l'est pas lui-même. Le moteur recharge le modèle à chaud (sans redémarrage).
# Usage (root, depuis n'importe où) : bash /opt/hermes/deploy/challenger.sh <dossier du modèle>
set -euo pipefail
NEW_DIR="${1:?usage : challenger.sh <dossier du modèle>}"
cd "${HERMES_ROOT:-/opt/hermes}"
HERMES="${HERMES_BIN:-.venv/bin/hermes}"
PY="${HERMES_PY:-.venv/bin/python}"
as_hermes() {
  if [ "$(id -u)" = 0 ] && id hermes >/dev/null 2>&1; then runuser -u hermes -- "$@"; else "$@"; fi
}
meta() { python3 -c "import json,sys;print(json.load(open(sys.argv[1])).get(sys.argv[2], ''))" "$1" "$2"; }
# Identité de la stratégie, recalculée par le code installé pour les deux modèles : le hash enregistré dépend de
# la version du code qui a entraîné le modèle (même stratégie, autre hash dès qu'un champ de configuration
# s'ajoute). À défaut (code ou fichier illisible), le hash enregistré.
ident() {
  "$PY" - "$1" 2>/dev/null <<'PY' || meta "$1/bundle.json" config_hash
import sys
from pathlib import Path

from hermes.config import HermesConfig
from hermes.research.run import config_hash

print(config_hash(HermesConfig.model_validate_json(Path(sys.argv[1], "config.json").read_text())))
PY
}
NEW="$NEW_DIR/bundle.json"
[ -f "$NEW" ] || { echo "pas de modèle : $NEW" >&2; exit 1; }
NEW_PROMOTED=$(meta "$NEW" promoted)
CUR_DIR=artifacts/models/champion
CUR_PROMOTED=False
CUR_ID=""
if [ -f "$CUR_DIR/bundle.json" ]; then
  CUR_PROMOTED=$(meta "$CUR_DIR/bundle.json" promoted)
  CUR_ID=$(ident "$CUR_DIR")
fi
if [ "$(ident "$NEW_DIR")" = "$CUR_ID" ] || [ "$NEW_PROMOTED" = "True" ] || [ "$CUR_PROMOTED" != "True" ]; then
  as_hermes "$HERMES" model install "$NEW_DIR" --to artifacts/models/champion
  echo "champion remplacé (promu=$NEW_PROMOTED, entraîné jusqu'au $(meta "$NEW" train_end))"
else
  echo "challenger d'une autre configuration non promu : le champion promu reste en place"
fi
