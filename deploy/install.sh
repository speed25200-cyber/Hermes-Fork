#!/usr/bin/env bash
# Installe / met à jour Hermes sur le VPS (Ubuntu 22.04+/Debian 12+). Idempotent.
# Usage : sudo bash deploy/install.sh <mode: paper|demo|live>
# Les clés OKX sont lues sur l'entrée standard (format KEY=VALUE), jamais passées en argument.
set -euo pipefail
MODE="${1:-paper}"
case "$MODE" in paper|demo|live) ;; *) echo "mode inconnu: $MODE" >&2; exit 2 ;; esac
APP=/opt/hermes
ETC=/etc/hermes

id hermes >/dev/null 2>&1 || useradd --system --home "$APP" --shell /usr/sbin/nologin hermes
mkdir -p "$APP" "$ETC" "$APP/state" "$APP/data" "$APP/artifacts/models" "$APP/reports"

export DEBIAN_FRONTEND=noninteractive
if ! command -v python3 >/dev/null || ! python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))'; then
  apt-get update -qq && apt-get install -y -qq python3 python3-venv python3-pip
fi
python3 -m venv --help >/dev/null 2>&1 || { apt-get update -qq && apt-get install -y -qq python3-venv; }

[ -d "$APP/.venv" ] || python3 -m venv "$APP/.venv"
"$APP/.venv/bin/pip" install -q --upgrade pip
"$APP/.venv/bin/pip" install -q -e "$APP"

# Secrets : fichier 0600 lisible par root et le service seulement. Lecture sur stdin si fournie.
if [ ! -t 0 ]; then
  umask 077
  TMP="$(mktemp)"
  cat > "$TMP"
  if [ -s "$TMP" ]; then
    grep -E '^(OKX_API_KEY|OKX_API_SECRET|OKX_API_PASSPHRASE|OKX_BASE_URL|HERMES_TELEGRAM_TOKEN|HERMES_TELEGRAM_CHAT)=' "$TMP" > "$ETC/hermes.env" || true
  fi
  rm -f "$TMP"
fi
touch "$ETC/hermes.env"
chmod 600 "$ETC/hermes.env"
chown root:hermes "$ETC/hermes.env"; chmod 640 "$ETC/hermes.env"

echo "$MODE" > "$ETC/mode"
install -m 644 "$APP/deploy/hermes@.service" /etc/systemd/system/hermes@.service
install -m 644 "$APP/deploy/hermes-retrain.service" /etc/systemd/system/hermes-retrain.service
install -m 644 "$APP/deploy/hermes-retrain.timer" /etc/systemd/system/hermes-retrain.timer
chown -R hermes:hermes "$APP"
systemctl daemon-reload

# Un compte, un moteur : on arrête les autres modes avant de démarrer celui demandé.
for m in paper demo live; do
  [ "$m" = "$MODE" ] || systemctl disable --now "hermes@$m" 2>/dev/null || true
done
systemctl enable hermes-retrain.timer >/dev/null
systemctl start hermes-retrain.timer

if [ -f "$APP/artifacts/models/champion/bundle.json" ]; then
  systemctl enable "hermes@$MODE" >/dev/null
  systemctl restart "hermes@$MODE"
  echo "service hermes@$MODE démarré"
else
  echo "aucun modèle installé : lancer d'abord l'entraînement (systemctl start hermes-retrain)"
fi
