# Matrice des exigences → modules → tests

Référence : cahier des charges « Instructions maître — Plateforme quantitative autonome OKX + JEV »
v1.0 du 18 septembre 2026. Chaque ligne cite la section, le module qui la porte et les tests qui la
prouvent (IDs `Txx` de `docs/test_matrix.md`). Les statuts de preuve sont dans `DELIVERY_REPORT.md`.

| § | Exigence | Module(s) | Tests |
|---|----------|-----------|-------|
| 1 | Principes non négociables (pas de look-ahead, pas de leakage, frais/spread obligatoires, LIVE désactivé, secrets hors Git, JEV sans clés OKX, ML ne contourne pas le risque, décisions journalisées, no-trade valide) | `domain`, `config`, `runtime.decision_loop`, `risk`, `execution.gateway` | T14–T17, T24, T64, T66, propriétés « pas d'ordre sans approbation » |
| 2 | Décision toutes les 60 s, horizons 1–15 min, `expected_net_edge`, incertitude | `runtime.scheduler`, `portfolio.forecasts`, `strategy` config | tests scheduler, edges |
| 3 | Univers dynamique point-in-time, sortie = reduce-only, biais qualifié | `data.universe`, `data.point_in_time` | T18 |
| 4 | Data engine WS/REST, snapshot+incrémental conforme, invalidation/resync | `data.collector`, `data.orderbook`, `exchange.okx.websocket_public`, `rest_public` | T05–T13 |
| 5 | Horodatages multiples, null jamais inventé | `domain.events.EventEnvelope`, `data.normalizer` | T15, T58 |
| 6 | Parquet + PostgreSQL, transactions durables | `data.archive`, `persistence` | intégration migrations, T34 |
| 7–11 | Feature engine PIT, microstructure, séries, cross-asset, dérivés | `features.*` | tests features, parité, T16, T26 |
| 12–15 | JEV : primitives Noul/Choice/Score, sources, questions étroites, anti-leakage | `jev.*`, `features.events` | T50–T58 |
| 16–19 | Baselines, targets, calibration, stacking OOF | `research.*` | T17, T19–T22 |
| 20 | Régimes filtrés au présent | `features.timeseries`/`cross_asset` (régimes), `research` | tests features |
| 21 | Expected edge engine (décomposition, incertitude) | `portfolio.forecasts`, `portfolio.costs` | T24, tests edges |
| 22–24 | Optimiseur contraint, long/short, sizing par budget de risque | `portfolio.optimizer`, `rounding`, `covariance` | T39–T42 |
| 25–26 | Risk Engine indépendant, kill switches | `risk.engine`, `kill_switch`, `watchdog`, `approvals` | T43–T49, T61 |
| 27–28 | Unités exactes, machine d'état d'ordre, idempotence locale | `domain.instruments`, `execution.state_machine`, `persistence.repositories` | T01–T03, T31–T36 |
| 29–30 | Maker/taker, smart execution, mesures | `execution.policies` | tests policies |
| 31 | Cancel All After, clés minimales, allowlist gateway | `execution.protections`, `exchange.okx.rest_private` | T60, tests allowlist |
| 32–33 | Backtest événementiel partagé, niveaux A/B/C, files maker | `backtest.*` | T27–T31, e2e golden |
| 34 | Horloges, causalité, départage stable | `domain.clocks`, `runtime.decision_loop`, `data.replay` | tests causalité, replay |
| 35 | Formules, invalidité (pas de zéro silencieux), normalisation sur train | `features.microstructure`, `research.splits` | tests features, T17 |
| 36 | Labels, unités, censure | `research.labels` | T19, T21 |
| 37 | Frais signés, funding, absence de double comptage | `accounting.funding`, `portfolio.costs` | T23–T26 |
| 38 | Ledger équilibré, réconciliation, HWM persistant | `accounting.ledger`, `pnl`, `risk.kill_switch` | T34, T44, T45 |
| 39 | Protocole statistique, walk-forward, période finale, bootstrap | `research.splits`, `evaluation`, `experiment_registry` | T17, T22, contrôles négatifs |
| 40 | Expérience A/B/C/D JEV | `research.jev_ablation` | e2e recherche |
| 41 | Stack verrouillée, processus distincts, outbox | `pyproject/uv.lock`, `compose.yaml`, `execution.outbox` | intégration outbox |
| 42 | Arborescence | dépôt | — |
| 43 | Contrats immuables, décision liée au hash | `domain.events` | T49 |
| 44 | Schéma PostgreSQL, contraintes, migrations | `persistence.models`, `migrations` | intégration |
| 45 | Contrats OKX, v, arrondis, NET/ISOLATED | `domain.instruments`, `portfolio.rounding`, `exchange.okx.adapter` | T01–T04, T40 |
| 46 | Catalogue d'API, capability manifest | `exchange.okx.capabilities`, `infra/capability_manifest.json`, `docs/api_contracts/` | contrat |
| 47 | Carnet : séquences, checksum déprécié 2026 | `data.orderbook` | T05–T13 |
| 48 | Auth HMAC, rate limits, timeouts, DEMO ≠ LIVE | `exchange.okx.authentication`, `rate_limits`, `rest_private` | T63, tests auth |
| 49–50 | Contrat JEV, validators, sources, mapping, cache, worker, dégradation | `jev.*` | T50–T58 |
| 51 | Formulation convexe, contraintes, fallback | `portfolio.optimizer` | T40–T42 |
| 52 | Ordres durables, UNKNOWN, un seul writer, redémarrage | `execution.gateway`, `outbox`, `leadership`, `runtime.startup` | T32, T33, T46, T47 |
| 53 | Budgets, scénarios | `risk.budgets`, `scenarios` | T43, scénarios |
| 54 | Protections, flatten | `execution.protections` | T59–T62 |
| 55 | Boucle décisionnelle, reason codes, NO_TRADE journalisé | `runtime.decision_loop`, `domain.reasons` | tests decision loop |
| 56 | Model registry, statuts, rollback | `research.training`, `experiment_registry` | tests registry |
| 57 | Politique d'exécution versionnée, capacité | `execution.policies` | tests policies |
| 58 | API opérateur, rôles, pas de route LIVE | `api.*` | T65 |
| 59 | Tableau de bord FR (interface Hermes conservée) | `frontend/` | node --test, smoke Playwright |
| 60 | Threat model, isolation des secrets | `docs/threat_model.md`, `compose.yaml`, `scripts/security_check.py` | T66 |
| 61 | Observabilité, métriques, alertes | `runtime.metrics`, `logging`, `alerts`, `health` | tests santé |
| 62 | Déploiement, sauvegarde, reprise | `infra/`, `deploy/`, runbooks | T67 (procédure) |
| 63 | Configuration stricte | `config.schema`, `loader` | tests config |
| 64 | Matrice de tests | `docs/test_matrix.md` | — |
| 65 | Performance, backpressure | `runtime.scheduler`, files bornées (`jev.worker`, `data.collector`) | T68 |
| 66 | Phases P0–P10 | `BUILD_STATUS.md` | — |
| 67 | CLI `okxq` et Make | `cli.py`, `Makefile` | smoke |
| 68 | Golden, smoke offline, scénarios de panne | `tests/fixtures/golden`, `tests/e2e`, `tests/chaos` | T69, T70 |
| 69 | Livrables documentaires | `docs/`, `README.md`, `DELIVERY_REPORT.md` | — |
| 70 | Budgets | `config.research`, `jev` budgets | tests worker |
| 71 | Gates LIVE, manifeste signé | `config.live_guard` | T64 |
| 72–77 | Hors périmètre, protocole de travail, revues, définition « construit » | `DELIVERY_REPORT.md`, `docs/live_readiness.md` | — |
