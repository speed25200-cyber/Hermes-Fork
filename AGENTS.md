# AGENTS.md — règles de développement et de sécurité du dépôt

Ce fichier s'adresse à tout agent (humain ou automatique) qui modifie ce dépôt. Il est normatif.
Le cahier des charges maître est résumé dans `docs/requirements_matrix.md` ; en cas de doute, ses
principes non négociables (§1) priment.

## 1. Langue et style

- Explications, commentaires, documentation, messages d'interface : **français**.
- Identifiants de code (modules, classes, fonctions, variables, clés JSON, colonnes SQL) : **anglais**.
- Python 3.12, typage strict (`mypy --strict` passe sur `src/okxq`), `ruff` sans erreur, ligne ≤ 110.
- Pas de `print` hors CLI ; journaux via `okxq.runtime.logging` (structlog, JSON).
- Pas de notebook, pas de code téléchargé, pas de pickle non fiable.

## 2. Unités, nombres, temps

- Montants, prix, quantités : `Decimal` via `okxq.domain.money.dec()` ; **jamais** de `float` pour une
  valeur monétaire. Les sorties de modèles (probabilités, mu) sont des flottants finis validés.
- Contrats ≠ quantité de base ≠ notionnel : utiliser `Contracts`, `BaseQty`, `Money` et les conversions de
  `okxq.domain.instruments` (`v = base_units_per_contract`).
- Arrondis avec direction nommée (`round_down_to_step`, `round_price_passive`...) ; une augmentation de
  risque s'arrondit vers zéro.
- Dates : `datetime` timezone-aware UTC uniquement (`ensure_utc`). Une date inconnue est `None`, jamais
  inventée. Horloge injectée (`Clock`) : aucun `datetime.now()` direct hors `SystemClock`.
- Causalité : `feature.available_at <= snapshot.cutoff_at <= decision.started_at`. Un événement n'existe
  pour le système qu'à partir de son `receive_ts`/`available_at`, jamais à son heure économique seule.

## 3. Contrats et frontières

- Les contrats de données vivent dans `okxq.domain.events` (pydantic, immuables, `extra="forbid"`).
  Ne pas en créer de parallèles ; les étendre là si nécessaire, avec test.
- Le modèle propose, le **Risk Engine** décide (`RiskDecision` liée au hash du payload normalisé) ; le
  **gateway unique** envoie. Aucun chemin de code ne crée un ordre sans `ApprovedOrder`.
- Secrets : uniquement par variables d'environnement, distribués au seul service concerné. JEV ne voit
  jamais les clés OKX ni les positions. Aucun secret dans YAML, logs, métriques, tests, fixtures.
- LIVE : désactivé par défaut ; seul `okxq.config.live_guard` peut l'autoriser, et uniquement avec un
  manifeste signé. Aucune option `--force`.

## 4. Tests

- `tests/unit` (hermétique, SQLite mémoire, pas de réseau), `tests/property` (Hypothesis),
  `tests/integration` (PostgreSQL via `OKXQ_TEST_DATABASE_URL`, marqué `integration`),
  `tests/contract` (fixtures fournisseurs), `tests/e2e` (parcours offline), `tests/chaos` (pannes).
- Les tests connectés sont marqués `connected` et **jamais** exécutés par défaut ni en LIVE.
- Nommer les tests de la matrice `test_T05_...` pour lier l'exigence (`docs/test_matrix.md`).
- Un mock ne court-circuite jamais la logique testée. Un test non exécuté reste `NOT_RUN`.
- Réseau interdit par construction dans les tests : la fixture `no_network` (autouse) fait échouer toute
  ouverture de socket sortante.

## 5. Persistance

- Schéma : `okxq.persistence.models` (source unique). Toute modification = migration Alembic versionnée,
  testée sur base vide et depuis la version précédente.
- Ordres, fills, ledger, outbox : écritures dans une transaction ; consommation idempotente ; versions
  optimistes ; aucune mise à jour aveugle.

## 6. Organisation et commandes

- `make setup | lint | typecheck | test | test-property | test-contract-offline | smoke-offline | build | ui-test | security-check`.
- CLI `okxq` (Typer) : chaque groupe de commandes vit dans `okxq/cli_cmds/<groupe>.py` et est monté par
  `okxq/cli.py`. Une commande non fonctionnelle sort avec le code 2 et le message `NOT_IMPLEMENTED` ; elle
  n'est jamais présentée comme fonctionnelle.
- `BUILD_STATUS.md` et `DECISIONS.md` sont mis à jour à chaque incrément significatif.

## 7. Ce qu'on ne fait jamais

- Réinitialisation destructive, force-push, réécriture d'historique.
- Backtest sans frais/spread/slippage ; sélection sur la période finale ; `train_test_split` aléatoire.
- Prétendre qu'un travail non exécuté l'a été ; masquer une divergence fournisseur par un parser permissif.
- Activer LIVE, augmenter un levier, changer un mode de compte, ou relâcher une limite pour faire passer
  un test ou une démonstration.
