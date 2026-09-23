#!/usr/bin/env bash
# Réentraînement hebdomadaire (lancé par hermes-retrain.timer, en root ; la recherche tourne en « hermes »).
# Même configuration que le champion : la nouvelle évaluation (plus de données) fait foi et le remplace
# toujours -- un champion promu qui échoue désormais la porte est rétrogradé (le moteur réel aplatit alors
# le livre) ; une réussite chanceuse ne devient jamais permanente.
# Configuration différente : le challenger ne remplace le champion que s'il est promu, ou si le champion
# ne l'est pas lui-même. Le moteur recharge le modèle à chaud.
set -euo pipefail
cd /opt/hermes
MODE="$(cat /etc/hermes/mode 2>/dev/null || echo paper)"
OUT="reports/auto/$(date -u +%Y-%m-%d)"
as_hermes() { runuser -u hermes -- "$@"; }
CONFIG="$(cat /etc/hermes/research_config 2>/dev/null || echo configs/research_30m_xl_lb_sres.yaml)"
as_hermes .venv/bin/hermes research run -c "$CONFIG" --out "$OUT" --n-null 40
meta() { python3 -c "import json,sys;print(json.load(open(sys.argv[1])).get(sys.argv[2], ''))" "$1" "$2"; }
NEW="$OUT/model/bundle.json"
NEW_PROMOTED=$(meta "$NEW" promoted)
CUR=artifacts/models/champion/bundle.json
CUR_PROMOTED=False
CUR_HASH=""
if [ -f "$CUR" ]; then
  CUR_PROMOTED=$(meta "$CUR" promoted)
  CUR_HASH=$(meta "$CUR" config_hash)
fi
if [ "$(meta "$NEW" config_hash)" = "$CUR_HASH" ] || [ "$NEW_PROMOTED" = "True" ] || [ "$CUR_PROMOTED" != "True" ]; then
  as_hermes .venv/bin/hermes model install "$OUT/model" --to artifacts/models/champion
  echo "champion remplacé (promu=$NEW_PROMOTED)"
else
  echo "challenger d'une autre configuration non promu : le champion promu reste en place"
fi
# Premier champion : démarrer le moteur du mode installé.
if [ -f "$CUR" ] && ! systemctl is-active --quiet "hermes@$MODE"; then
  systemctl enable --now "hermes@$MODE" && echo "hermes@$MODE démarré"
fi
