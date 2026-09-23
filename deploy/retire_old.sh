#!/usr/bin/env bash
# Efface les systèmes précédents du VPS :
#   - la plateforme okxq (Docker compose dans /opt/okxq : conteneurs, images, volumes dont sa base PostgreSQL) ;
#   - l'ancien moteur Node « hermes » (/root/hermes, services hermes.service, hermes-perles.*, hermes-research.*,
#     hermes-dashboard.service -- sans « @ »).
# Ne touche jamais : /opt/hermes, /etc/hermes, les services hermes@*, hermes-dashboard@*, hermes-retrain.*, SSH.
#
# Garde-fou : si une clé OKX est posée dans un ancien fichier d'environnement, l'ancien moteur a pu ouvrir des
# positions réelles, et un moteur effacé ne les ferme pas. L'effacement est alors refusé (PURGE_WITH_KEYS=1
# pour passer outre, après vérification des positions sur OKX).
set -u

echo "== inventaire avant effacement"
keys=0
for f in /opt/okxq/env/gateway.env /opt/okxq/env/*.env /root/hermes/.env; do
  [ -f "$f" ] || continue
  if grep -Eq '^(OKX_API_KEY|OKX_KEY|OKX_API_SECRET|OKX_SECRET)=.+' "$f"; then
    echo "  clé OKX présente dans $f"
    keys=1
  fi
done
[ "$keys" = 0 ] && echo "  aucune clé OKX dans les anciens fichiers d'environnement"
for d in /opt/okxq /root/hermes; do [ -d "$d" ] && du -sh "$d" 2>/dev/null; done
if command -v docker >/dev/null && systemctl is-active --quiet docker 2>/dev/null; then
  docker ps -a --format '  conteneur {{.Names}} ({{.Status}})' 2>/dev/null | head -30
fi
systemctl list-units --all --no-legend --plain 'hermes*' 'okxq*' 2>/dev/null | sed 's/^/  /'

if [ "$keys" = 1 ] && [ "${PURGE_WITH_KEYS:-0}" != 1 ]; then
  echo "REFUS : une clé OKX est posée dans un ancien système. Vérifier (et fermer si besoin) ses positions sur OKX,"
  echo "        puis relancer avec PURGE_WITH_KEYS=1. Rien n'a été effacé."
  exit 1
fi

echo "== effacement de okxq"
if command -v docker >/dev/null && systemctl is-active --quiet docker 2>/dev/null; then
  if [ -f /opt/okxq/compose.yaml ]; then
    (cd /opt/okxq && docker compose down --volumes --rmi all --remove-orphans >/dev/null 2>&1) || true
  fi
  # Ce que compose n'aurait pas retiré (fichiers d'environnement absents, projet renommé).
  docker ps -aq --filter label=com.docker.compose.project=okxq | xargs -r docker rm -f >/dev/null 2>&1
  docker volume ls -q --filter label=com.docker.compose.project=okxq | xargs -r docker volume rm >/dev/null 2>&1
  docker image prune -af >/dev/null 2>&1 || true
  docker builder prune -af >/dev/null 2>&1 || true
fi
rm -rf /opt/okxq && echo "  /opt/okxq effacé"

echo "== effacement de l'ancien moteur Node"
for u in hermes.service hermes-live.service hermes-perles.service hermes-perles.timer hermes-research.service \
  hermes-research.timer hermes-dashboard.service okxq.service; do
  if [ -e "/etc/systemd/system/$u" ] || systemctl list-unit-files "$u" --no-legend 2>/dev/null | grep -q .; then
    systemctl disable --now "$u" >/dev/null 2>&1 || true
    rm -f "/etc/systemd/system/$u"
    echo "  $u retiré"
  fi
done
pkill -f "node .*app/main.js" 2>/dev/null && echo "  processus Node arrêté" || true
rm -rf /root/hermes && echo "  /root/hermes effacé"
if crontab -l 2>/dev/null | grep -Eq 'okxq|/root/hermes'; then
  crontab -l | grep -Ev 'okxq|/root/hermes' | crontab - && echo "  tâches cron des anciens systèmes retirées"
fi
systemctl daemon-reload
systemctl reset-failed 2>/dev/null || true

# Docker ne servait qu'à okxq : sans conteneur, on l'arrête (il reste installé).
if command -v docker >/dev/null && [ -z "$(docker ps -aq 2>/dev/null)" ]; then
  systemctl disable --now docker.service docker.socket containerd.service >/dev/null 2>&1 || true
  echo "  Docker arrêté (plus aucun conteneur)"
fi

echo "== après effacement"
systemctl list-units --all --no-legend --plain 'hermes*' 'okxq*' 2>/dev/null | sed 's/^/  /'
ss -tlnp 2>/dev/null | awk 'NR>1 {print "  écoute", $4}' | sort -u
free -m | awk '/Mem:/ {print "  mémoire utilisée", $3, "Mo sur", $2}'
echo "anciens systèmes effacés"
