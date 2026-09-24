#!/usr/bin/env bash
# Déploiement direct depuis un poste (alternative au workflow GitHub « Deploy »).
# Usage : VPS=178.104.191.79 MODE=paper TRAIN=1 bash deploy/deploy.sh
# Les clés OKX éventuelles sont lues dans l'environnement local (OKX_API_KEY, OKX_API_SECRET,
# OKX_API_PASSPHRASE) et transmises par l'entrée standard de ssh, jamais en argument.
set -euo pipefail
VPS="${VPS:?adresse du VPS (VPS=...)}"
MODE="${MODE:-paper}"
TRAIN="${TRAIN:-1}"
SSH="ssh -o StrictHostKeyChecking=accept-new root@$VPS"
cd "$(dirname "$0")/.."

if [ "$MODE" != "paper" ] && { [ -z "${OKX_API_KEY:-}" ] || [ -z "${OKX_API_SECRET:-}" ] || [ -z "${OKX_API_PASSPHRASE:-}" ]; }; then
  echo "mode $MODE : OKX_API_KEY / OKX_API_SECRET / OKX_API_PASSPHRASE requis dans l'environnement" >&2
  exit 2
fi

$SSH 'mkdir -p /opt/hermes'
# Copie du code suivi par git uniquement (pas de données, d'état ni de secrets).
git ls-files -z | tar --null -T - -czf - | $SSH 'tar -xzf - -C /opt/hermes'
# Version du code installé, que le réentraînement mensuel (workflow Retrain) reprend. Un arbre modifié n'a pas de
# version : le fichier est retiré et le réentraînement refuse de tourner tant qu'on n'a pas redéployé proprement.
if [ -z "$(git status --porcelain)" ]; then
  git rev-parse HEAD | $SSH 'cat > /opt/hermes/REVISION'
else
  $SSH 'rm -f /opt/hermes/REVISION'
fi
{
  [ -n "${OKX_API_KEY:-}" ] && printf 'OKX_API_KEY=%s\nOKX_API_SECRET=%s\nOKX_API_PASSPHRASE=%s\n' \
    "$OKX_API_KEY" "$OKX_API_SECRET" "$OKX_API_PASSPHRASE"
  [ -n "${HERMES_TELEGRAM_TOKEN:-}" ] && printf 'HERMES_TELEGRAM_TOKEN=%s\nHERMES_TELEGRAM_CHAT=%s\n' \
    "$HERMES_TELEGRAM_TOKEN" "${HERMES_TELEGRAM_CHAT:-}"
  [ -n "${HERMES_DASHBOARD_TOKEN:-}" ] && printf 'HERMES_DASHBOARD_TOKEN=%s\n' "$HERMES_DASHBOARD_TOKEN"
  true
} | $SSH "bash /opt/hermes/deploy/install.sh $MODE"
if [ "$TRAIN" = "1" ]; then
  $SSH 'systemctl start --no-block hermes-retrain.service && echo "entraînement lancé (≈ 1 h) ; le moteur démarre ensuite"'
fi
$SSH 'bash -s' < deploy/status.sh
