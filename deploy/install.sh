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
    grep -E '^(OKX_API_KEY|OKX_API_SECRET|OKX_API_PASSPHRASE|OKX_BASE_URL|HERMES_TELEGRAM_TOKEN|HERMES_TELEGRAM_CHAT|HERMES_DASHBOARD_TOKEN|HERMES_DASHBOARD_PORT)=' "$TMP" > "$ETC/hermes.env" || true
  fi
  rm -f "$TMP"
fi
touch "$ETC/hermes.env"
chmod 600 "$ETC/hermes.env"
chown root:hermes "$ETC/hermes.env"; chmod 640 "$ETC/hermes.env"

echo "$MODE" > "$ETC/mode"
install -m 644 "$APP/deploy/hermes@.service" /etc/systemd/system/hermes@.service
install -m 644 "$APP/deploy/hermes-dashboard@.service" /etc/systemd/system/hermes-dashboard@.service
install -m 644 "$APP/deploy/hermes-retrain.service" /etc/systemd/system/hermes-retrain.service
install -m 644 "$APP/deploy/hermes-retrain.timer" /etc/systemd/system/hermes-retrain.timer
chown -R hermes:hermes "$APP"
systemctl daemon-reload

# Un compte, un moteur : on arrête les autres modes avant de démarrer celui demandé.
for m in paper demo live; do
  [ "$m" = "$MODE" ] || systemctl disable --now "hermes@$m" 2>/dev/null || true
done
# Le réentraînement (walk-forward complet) demande bien plus de mémoire qu'un petit VPS : en dessous de
# 12 Go il se fait sur un runner GitHub (workflow Research) et le modèle est installé par le déploiement.
MEM_GB=$(awk '/MemTotal/ {print int($2 / 1048576)}' /proc/meminfo)
if [ "$MEM_GB" -ge 12 ]; then
  systemctl enable hermes-retrain.timer >/dev/null
  systemctl start hermes-retrain.timer
else
  systemctl disable --now hermes-retrain.timer 2>/dev/null || true
  systemctl reset-failed hermes-retrain.service 2>/dev/null || true
  echo "mémoire ${MEM_GB} Go < 12 Go : réentraînement hebdomadaire sur runner GitHub, pas sur ce VPS"
fi

for m in paper demo live; do
  [ "$m" = "$MODE" ] || systemctl disable --now "hermes-dashboard@$m" 2>/dev/null || true
done
if grep -q '^HERMES_DASHBOARD_TOKEN=.' "$ETC/hermes.env"; then
  DPORT=$(sed -n 's/^HERMES_DASHBOARD_PORT=//p' "$ETC/hermes.env" | tail -1)
  DPORT=${DPORT:-8899}
  # Pare-feu actif (ufw) : ouvrir le port du tableau de bord (protégé par son jeton), fermer l'ancien 8900.
  if command -v ufw >/dev/null && ufw status 2>/dev/null | grep -q "Status: active"; then
    ufw allow "$DPORT/tcp" >/dev/null && echo "ufw : port $DPORT ouvert"
    [ "$DPORT" = 8900 ] || ufw delete allow 8900/tcp >/dev/null 2>&1 || true
  fi
  systemctl enable "hermes-dashboard@$MODE" >/dev/null && systemctl restart "hermes-dashboard@$MODE"
  echo "tableau de bord : http://<vps>:$DPORT/?token=<HERMES_DASHBOARD_TOKEN>"
else
  echo "tableau de bord non exposé : secret HERMES_DASHBOARD_TOKEN absent (accès local : hermes live dashboard)"
fi

if [ -f "$APP/artifacts/models/champion/bundle.json" ]; then
  systemctl enable "hermes@$MODE" >/dev/null
  systemctl restart "hermes@$MODE"
  echo "service hermes@$MODE démarré"
else
  echo "aucun modèle installé : déployer avec model_run (modèle entraîné sur runner), ou entraîner ici si la mémoire suffit"
fi
