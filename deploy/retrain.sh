#!/usr/bin/env bash
# Réentraînement hebdomadaire sur un VPS d'au moins 12 Go (lancé par hermes-retrain.timer, en root ; la
# recherche tourne en « hermes »). En dessous, le workflow GitHub « Retrain » entraîne sur un runner. Dans les
# deux cas, deploy/challenger.sh décide si le nouveau modèle remplace le champion.
set -euo pipefail
cd /opt/hermes
MEM_GB=$(awk '/MemTotal/ {print int($2 / 1048576)}' /proc/meminfo)
if [ "$MEM_GB" -lt 12 ] && [ "${HERMES_FORCE_RETRAIN:-0}" != 1 ]; then
  echo "mémoire ${MEM_GB} Go < 12 Go : réentraînement refusé ici (il mettrait le moteur en danger) ; le workflow Retrain entraîne sur un runner GitHub"
  exit 0
fi
MODE="$(cat /etc/hermes/mode 2>/dev/null || echo paper)"
OUT="reports/auto/$(date -u +%Y-%m-%d)"
as_hermes() { runuser -u hermes -- "$@"; }
CONFIG="$(cat /etc/hermes/research_config 2>/dev/null || echo configs/research_30m_xl_lb_sres.yaml)"
as_hermes .venv/bin/hermes research run -c "$CONFIG" --out "$OUT" --n-null 40
CUR=artifacts/models/champion/bundle.json
bash deploy/challenger.sh "$OUT/model"
# Premier champion : démarrer le moteur du mode installé.
if [ -f "$CUR" ] && ! systemctl is-active --quiet "hermes@$MODE"; then
  systemctl enable --now "hermes@$MODE" && echo "hermes@$MODE démarré"
fi
