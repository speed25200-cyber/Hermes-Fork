# BUILD_STATUS.md — état de construction (§73)

Mis à jour à chaque incrément. Permet la reprise d'une session sans mémoire conversationnelle.

## Phase courante

P0/P1 livrés ; P2–P8 en construction parallèle par modules ; P9/P10 à venir.

## Fonctionnalités implémentées

- P0 : arborescence §42, `pyproject.toml` + `uv.lock` (versions verrouillées), `Makefile`, `.env.example`,
  `configs/` (base, paper, shadow, demo, live.disabled, risk.research, sources, questions JEV v1,
  experiment.example), `AGENTS.md`, schéma PostgreSQL complet (30 tables, `okxq.persistence.models`).
- P1 : `okxq.domain` (money/Decimal, unités Contracts/BaseQty/Money, arrondis dirigés, instruments
  point-in-time avec rejet inverse/options/USDC, positions avec scission réduction/flip, contrats §43,
  protocoles, horloges système/simulée, ids/hachage canonique), `okxq.config` (schéma strict,
  extends/fusion, refus des secrets en YAML, garde LIVE par manifeste signé HMAC), CLI `okxq config
  validate` et `okxq doctor --offline`, interface commune `okxq.exchange.base.ExchangeAdapter`.

## Tests exécutés

- `pytest tests/unit` : 34 tests (money, instruments T01–T04, positions T39, config, garde LIVE T64,
  contrats T49). `ruff` et `mypy --strict` : 0 erreur.

## Défauts connus

- Aucun module de P2–P8 n'est encore relié : `okxq` n'expose que `config` et `doctor`.

## Prérequis externes

- PostgreSQL 16 pour les tests d'intégration (`OKXQ_TEST_DATABASE_URL`).
- Clés OKX DEMO et clé TypeSafe uniquement pour les validations connectées (jamais en CI, jamais LIVE).

## Prochaine action concrète

Intégrer les modules P2–P8, câbler `okxq.runtime` (scheduler 60 s, startup, supervisor), faire passer
`make smoke-offline`, puis rédiger le dossier de livraison (P9/P10).
