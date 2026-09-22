#!/usr/bin/env bash
# Réentraînement hebdomadaire (lancé par hermes-retrain.timer, en root ; la recherche tourne en « hermes »).
# Le nouveau modèle (challenger) remplace le champion seulement s'il est promu par la porte statistique,
# ou si le champion actuel ne l'est pas lui-même (modes papier/démo). Le moteur le recharge à chaud.
set -euo pipefail
cd /opt/hermes
MODE="$(cat /etc/hermes/mode 2>/dev/null || echo paper)"
OUT="reports/auto/$(date -u +%Y-%m-%d)"
as_hermes() { runuser -u hermes -- "$@"; }
as_hermes .venv/bin/hermes research run -c configs/research.yaml --out "$OUT" --n-null 40
flag() { python3 -c "import json,sys;print(json.load(open(sys.argv[1])).get('promoted', False))" "$1"; }
NEW_PROMOTED=$(flag "$OUT/model/bundle.json")
CUR=artifacts/models/champion/bundle.json
CUR_PROMOTED=False
[ -f "$CUR" ] && CUR_PROMOTED=$(flag "$CUR")
if [ "$NEW_PROMOTED" = "True" ] || [ "$CUR_PROMOTED" != "True" ]; then
  as_hermes .venv/bin/hermes model install "$OUT/model" --to artifacts/models/champion
  echo "champion remplacé (promu=$NEW_PROMOTED)"
else
  echo "challenger non promu : le champion promu reste en place"
fi
# Premier champion : démarrer le moteur du mode installé.
if [ -f "$CUR" ] && ! systemctl is-active --quiet "hermes@$MODE"; then
  systemctl enable --now "hermes@$MODE" && echo "hermes@$MODE démarré"
fi
