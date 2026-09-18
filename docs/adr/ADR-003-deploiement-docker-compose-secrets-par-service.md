# ADR-003 — Déploiement : Docker Compose, un conteneur par rôle, secrets par service

**Statut** : accepté (18 septembre 2026).

**Contexte** : §41 (processus distincts ne partageant pas tous les secrets), §60 (les clés OKX
n'existent que dans le composant de signature), §62 (conteneurs non-root, PostgreSQL non exposé,
sauvegardes). Le VPS existant (Hetzner, Ubuntu) exécutait Hermes sous systemd avec un `.env` unique.

**Décision** : `compose.yaml` définit `postgres`, `api`, `collector`, `strategy`, `risk`, `gateway`,
`jev-worker`, chacun avec son `env_file` dans `/opt/okxq/env/<service>.env` (chmod 600). Seul
`gateway.env` porte `OKX_API_*` ; seul `jev-worker.env` porte `TYPESAFE_API_KEY` ; seul `api.env` porte
`OPERATOR_AUTH_SECRET`. PostgreSQL n'est joignable que sur le réseau compose. L'installateur
`deploy/install.sh` est idempotent et ne journalise aucun secret.

**Alternatives écartées** : systemd + uv sans conteneurs (plus léger, mais un seul environnement
partagé) ; Kubernetes (hors périmètre première version, §41).

**Conséquences** : la migration depuis Hermes (`deploy/uninstall_hermes.sh`) est destructive et
gardée par une confirmation explicite et un relevé des positions ouvertes ; les clés OKX de l'ancien
`.env` ne sont pas reprises (elles se posent via « VPS status » dans le seul `gateway.env`).
