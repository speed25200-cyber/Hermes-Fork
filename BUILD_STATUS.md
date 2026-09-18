# BUILD_STATUS.md — état de construction (§73)

Mis à jour à chaque incrément. Permet la reprise d'une session sans mémoire conversationnelle.
Dernière mise à jour : 18 septembre 2026, socle + runtime poussés sur `claude/hermes-fork-migration-m4wcbm`.

Légende des statuts : **IMPLÉMENTÉ** (code présent, lint/mypy verts) · **TESTÉ HORS LIGNE** (tests
hermétiques exécutés et verts) · **EN COURS** (agent de construction actif) · **BLOQUÉ : accès manquant**
(nécessite clé, réseau, compte ou machine non disponibles dans cette session) · **NON COMMENCÉ**.

## Vue par phase (§66)

| Phase | Contenu | Statut | Preuve |
|---|---|---|---|
| P0 Audit et contrats | arborescence §42, `pyproject.toml` + `uv.lock`, `Makefile`, `.env.example`, configs, `AGENTS.md`, `DECISIONS.md`, `docs/requirements_matrix.md`, `docs/architecture.md`, `docs/api_contracts.md` (index), schéma 30 tables | TESTÉ HORS LIGNE | `okxq config validate` sur 7 profils ; `memory_engine()` crée les 30 tables |
| P1 Domaine, config, sécurité initiale | `okxq.domain` (Decimal/unités, arrondis dirigés, instruments PIT avec rejets T02, positions à coût d'ouverture exact, contrats §43, protocoles, horloges, ids), `okxq.config` (schéma strict, extends, refus des secrets, garde LIVE par manifeste signé), CLI `config validate`/`doctor`, `live preflight|run` (refus sans manifeste) | TESTÉ HORS LIGNE | 58 tests `tests/unit` + `tests/property` (T01–T04, T33 séquence, T39, T49, T64) ; `ruff`, `mypy --strict` verts |
| Runtime | `runtime.scheduler` (frontières UTC 60 s, skip/coalesce, pas de rafale), `runtime.decision_loop` (§55, NO_TRADE journalisé, risque puis gateway), `runtime.startup` (§52.4) / arrêt (§62), `runtime.supervisor` (actions opérateur, santé) | TESTÉ HORS LIGNE | 21 tests runtime |
| P2 Données et replay | carnet OKX (seq/checksum 2026), normalisation, qualité, archive Parquet, PIT, univers, replay, REST/WS publics, capability manifest | EN COURS (agent) | — |
| P3 Comptabilité, simulateur, golden | ledger, PnL, funding, coûts, fills/files/latence, exchange virtuel, moteur de backtest, jeu golden §68, `okxq replay run` | EN COURS (agent) | — |
| P4 Recherche | features PIT (parité batch/direct), labels, splits imbriqués, baselines/LightGBM, calibration, stacking OOF, évaluation, registre, ablation JEV A/B/C/D, `Predictor` | EN COURS (agent) | — |
| P5 JEV isolé | schémas/validators, client à deadline, sources SSRF-safe, mapping PIT, cache, worker borné, corpus sémantique, `okxq jev validate-fixtures` | EN COURS (agent) | — |
| P6 Portefeuille et risque | edges, covariance, optimiseur CVXPY §51, arrondis/deltas, Risk Engine, approbations liées au hash, kill switches persistés, watchdog, `okxq risk status`/`control` | EN COURS (agent) | — |
| P7 Exécution et DEMO | dépôts transactionnels, migrations Alembic, state machine, outbox, bail/fencing, gateway unique, réconciliation, protections, politiques, auth HMAC, REST/WS privés, adaptateur OKX, `okxq db migrate` | EN COURS (agent) ; validation DEMO connectée : BLOQUÉ : accès manquant (aucune clé DEMO) | — |
| P8 Interface et exploitation | API FastAPI + auth/rôles/CSRF, interface Hermes conservée et étendue (Décisions, Recherche, JEV, Risque), logs JSON masqués, métriques §61, santé, alertes, Dockerfile, `compose.yaml`, CI, sauvegardes, runbooks startup/shutdown/backup/key_rotation, threat model, ADR-001 | EN COURS (agent) ; déjà livrés : installateur VPS, workflows `deploy-vps.yml`/`vps-status.yml`, runbooks unknown_order/book_resync/jev_outage/emergency_flatten/reconcile_pnl/model_rollback, ADR-002..004 | — |
| Composition | `okxq.runtime.composition` (câblage des rôles collector/strategy/risk/gateway/jev-worker/api), `okxq paper|shadow|demo run`, `make smoke-offline`, `okxq backtest run`, `okxq reports export` | NON COMMENCÉ (dépend de P2–P8) | `okxq paper run` sort NOT_IMPLEMENTED (code 2) |
| P9 Dossier prospectif | collecte SHADOW, comparaison A/B/C, suivi de calibration, rapports automatiques | NON COMMENCÉ | — |
| P10 Dossier LIVE | `docs/live_readiness.md` (gates NON franchis), `scripts/sign_approval_manifest.py`, garde LIVE | IMPLÉMENTÉ (mécanique) ; activation : BLOQUÉ par conception (aucune preuve, aucune autorisation) | T64 |
| Migration VPS | `deploy/install.sh`, `deploy/uninstall_hermes.sh` (confirmation EFFACER + relevé des positions), `deploy/preflight.sh`, `deploy/etat.sh`, workflows | IMPLÉMENTÉ ; exécution : à déclencher après composition (nécessite `VPS_PASSWORD` du dépôt Hermes) | `bash -n` et YAML valides |

## Tests exécutés (dernier run)

`.venv/bin/pytest -q tests/unit tests/property` → 58 passed. `ruff check`, `ruff format --check`,
`mypy --strict` → 0 erreur. Aucun test connecté exécuté (NOT_RUN par construction).

## Défauts connus

- La CLI n'expose que `config`, `doctor`, `paper/shadow/demo/live` (NOT_IMPLEMENTED tant que la
  composition n'est pas livrée) ; les autres groupes apparaissent à la fusion des modules.

## Prérequis externes (accès manquants dans cette session)

- Clés OKX DEMO + profil de région : validation connectée du connecteur privé (P7) et `okxq demo preflight`.
- Clé TypeSafe : premier appel réel JEV (P5) ; sans elle, `jev status` = NOT_RUN.
- Historique de marché réel : la recherche (P4) ne dispose que de jeux synthétiques → aucune preuve d'alpha.
- Secret `VPS_PASSWORD` (dépôt Hermes) et branche par défaut : le workflow de migration est dispatchable
  depuis le dépôt Hermes (fichier existant sur `main`) sur la branche de migration.

## Prochaine action concrète

Fusionner les sept branches d'agents, résoudre les conflits de contrats, écrire
`okxq.runtime.composition`, faire passer `make lint typecheck test smoke-offline`, générer
`docs/test_matrix.md` depuis JUnit, rédiger `DELIVERY_REPORT.md`, puis pousser les deux dépôts et
déclencher la migration VPS.
