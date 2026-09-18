# okx-quant-jev — plateforme quantitative autonome OKX + JEV

Recherche, PAPER, SHADOW et DEMO sur les perpetual swaps linéaires USDT d'OKX, avec JEV (TypeSafe AI)
comme composant sémantique isolé. Décision toutes les 60 secondes, surveillance et protection
continues. **LIVE est désactivé par défaut** et ne s'active ni par l'interface ni par un booléen : il
exige un manifeste d'approbation signé et trois gates (`docs/live_readiness.md`).

Ce dépôt remplace le moteur Hermes (Node) ; il en conserve l'interface web (`frontend/`), étendue de
quatre vues (Décisions, Recherche, JEV, Risque/exploitation).

> Aucune rentabilité n'est annoncée. Le système mesure ; il ne promet pas. Voir `DELIVERY_REPORT.md`
> pour ce qui a été testé hors ligne, ce qui n'a pas été testé avec les fournisseurs, et ce qui manque.

## Démarrage sûr (poste de développement)

```bash
make setup                                   # uv sync sur uv.lock (Python 3.12)
uv run okxq config validate --config configs/paper.yaml
uv run okxq doctor --config configs/paper.yaml --offline
make lint typecheck test                     # hermétique : aucun réseau, SQLite mémoire
make smoke-offline                           # parcours complet §68.2 sur le jeu golden
```

Aucune de ces commandes n'appelle un endpoint privé d'exchange. Les tests connectés sont opt-in
(`-m connected`) et ne tournent jamais en CI ni en LIVE.

## Modes

| Mode | Fichier | Données | Ordres | Clés |
|---|---|---|---|---|
| RESEARCH | `configs/risk.research.yaml` | archives | simulateur | aucune |
| PAPER | `configs/paper.yaml` | OKX publiques | simulateur local | aucune |
| SHADOW | `configs/shadow.yaml` | OKX publiques | aucun (décisions archivées) | aucune |
| DEMO | `configs/demo.yaml` | OKX publiques + privées démo | compte démo OKX | OKX démo (gateway seul) |
| LIVE | `configs/live.disabled.yaml` | — | **désactivé** | — |

## Exploitation (VPS, Docker Compose)

Un processus par rôle (`collector`, `strategy`, `risk`, `gateway`, `jev-worker`, `api`) et PostgreSQL
non exposé. Chaque service ne reçoit que son secret (`infra/env/*.example`). Déploiement par le workflow
« Deploy okx-quant-jev to VPS » ; état par « VPS status » ; installateur `deploy/install.sh`.

Interface : `http://<vps>:8899/?key=<clé opérateur>` (clé posée une fois, puis cookie de session).

## Commandes

```bash
uv run okxq config validate --config configs/paper.yaml
uv run okxq doctor --config configs/paper.yaml --offline
uv run okxq db migrate --config configs/paper.yaml
uv run okxq data inspect --manifest data/manifest.json
uv run okxq replay run --dataset tests/fixtures/golden --config configs/paper.yaml
uv run okxq research prepare|train|evaluate|compare-jev --experiment configs/experiment.example.yaml
uv run okxq backtest run --experiment configs/experiment.example.yaml
uv run okxq paper run --config configs/paper.yaml
uv run okxq shadow run --config configs/shadow.yaml
uv run okxq demo preflight --config configs/demo.yaml
uv run okxq jev validate-fixtures
uv run okxq risk status --config configs/paper.yaml
uv run okxq control pause --config configs/paper.yaml --reason operator_request
uv run okxq reports export --run-id RUN_ID --output reports/
```

Une commande non fonctionnelle sort avec `NOT_IMPLEMENTED` (code 2) : elle n'est jamais présentée
comme fonctionnelle. `BUILD_STATUS.md` liste l'état réel.

## Documentation

`docs/architecture.md`, `docs/api_contracts.md`, `docs/data_dictionary.md`, `docs/strategy_research.md`,
`docs/test_matrix.md`, `docs/threat_model.md`, `docs/live_readiness.md`, `docs/runbooks/`, `docs/adr/`,
`AGENTS.md`, `DECISIONS.md`, `BUILD_STATUS.md`, `DELIVERY_REPORT.md`.

## Limites à ne pas masquer

Pas d'historique L2 réel collecté dans cette session ; pas d'appel réel à TypeSafe ni à OKX privé ;
aucune preuve d'alpha ; les résultats sur données synthétiques ne valent rien comme preuve de
rentabilité. Risques d'exchange, de collatéral, de liquidation/ADL, de concentration et
d'indisponibilité subsistent quelle que soit la qualité de l'architecture.
