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

| Commande | Résultat |
| --- | --- |
| `pytest -q -m "not integration and not connected"` | 281 passed |
| `ruff check` / `ruff format --check` (src, tests, scripts) | 0 erreur |
| `mypy` (strict, `src/okxq`) | 0 erreur sur 144 modules |
| `node --test "frontend/tests/*.test.js"` | 8 passed |
| `make smoke-offline` | 21 contrôles verts (4 régimes × 2 scénarios, reproductibilité T70) |
| `pytest tests/e2e/test_ui_smoke.py -m e2e` | **1 passed** — interface vérifiée dans Chromium |

Aucun test connecté exécuté : NOT_RUN par construction (ni clé OKX ni clé TypeSafe dans cette session).

### Smoke navigateur : de NOT_RUN à PASS

Le smoke d'interface sautait silencieusement. Motif réel obtenu avec `-rs` : le paquet Playwright
épinglé réclame la révision Chromium 1243 alors que l'environnement fournit la 1194. Il lance
désormais le binaire préinstallé (`PLAYWRIGHT_BROWSERS_PATH`, `--no-sandbox`) sans aucun
téléchargement, et saute encore **avec son motif** si aucun binaire n'est utilisable.

Ce test, une fois réellement exécuté, a révélé trois défauts que rien d'autre ne voyait.

## Défauts trouvés et corrigés pendant cette vérification

| Défaut | Effet observable | Correction |
| --- | --- | --- |
| Le bloc de risque était publié sous `halts` par `/api/v1/system/status` tandis que `pages.js` lisait `risk` | La page Risque affichait « Non disponible » sur ses cinq lignes alors que la donnée existait | `pages.js` lit `etat.halts` ; contrat verrouillé des deux côtés |
| `/api/v1/experiments` ne publiait ni `items` ni `jev_variants` | Pages Recherche et panneau A/B JEV vides sans aucune erreur | l'endpoint publie `items` (vocabulaire commun) et `jev_variants` dérivés des rapports d'ablation |
| `/api/v1/jev/status` ne publiait pas `available`, `age_seconds`, `latency_p95_ms`, `errors`, `daily_spend_usd` ; `jev_sources()` ni `age_seconds` ni `status` | Sept tuiles JEV et deux colonnes de sources bloquées sur « Non disponible » | l'API reprend le vocabulaire de `JevStatus` du worker ; les compteurs qui n'existent qu'en mémoire du worker sont relayés depuis son instantané, sinon `None` |
| `configure_logging` remettait les handlers des loggers tiers mais pas leur **niveau** | Un niveau posé par uvicorn écartait les enregistrements AVANT le handler masqué : journal muet là où on le croyait filtré, et le masquage dépendait de l'ordre des imports | niveau remis à NOTSET ; test T66 dédié qui échoue sans le correctif ; uvicorn démarré avec `log_config=None` |

Les deux premiers défauts étaient invisibles en Python : la page se rendait sans erreur, chaque valeur
devenait simplement « Non disponible ». `tests/contract/test_ui_api_contract.py` les verrouille
désormais **sans navigateur** (l'API publie les clés lues, et `pages.js` cite bien ces noms), pour que
le garde-fou tienne aussi là où aucun Chromium n'est disponible.

## Défauts trouvés et corrigés en écrivant le runtime et les groupes CLI

| Défaut | Effet observable | Correction |
| --- | --- | --- |
| `.gitignore` portait `data/` et `runtime/` sans ancre de racine | `src/okxq/data/` et `src/okxq/runtime/` étaient exclus : seize modules (carnet, normalisation, qualité, archive, replay, univers, collecteur, journalisation, santé, métriques, alertes, ordonnanceur, boucle, démarrage, superviseur) fonctionnaient sur le disque local **sans avoir jamais été versionnés** | motifs ancrés `/data/` et `/runtime/` ; les 16 modules entrent dans l'historique |
| `decision_loop` contrôlait `available_at <= cutoff_at` sur les vecteurs de features | Le contrat impose déjà `available_at >= cutoff_at` : l'égalité stricte était donc forcée, et tout fournisseur horodatant réellement sa production aurait fait échouer **chaque** décision | le contrôle porte sur `cutoff_at`, qui est le champ qui porte la causalité (borne des données utilisées) |
| Le contrôle de causalité des prévisions avait la bonne condition et un corps vide (`pass`) | Une prévision datée avant la borne de ses propres données traversait la boucle en silence — exactement la fuite temporelle que la causalité doit exclure | le contrôle refuse la décision (FAILED journalisé avec `CAUSALITY_VIOLATION`) |
| `require_role` cherchait la piste d'audit sur `app.state.audit`, que rien ne renseignait | Sa branche d'audit était protégée par `if audit is not None` et ne s'exécutait jamais : un **lecteur authentifié** sondant une commande privilégiée était refusé sans laisser de trace, alors qu'un anonyme en laissait une | `app.state.audit` renseigné dans `create_app` |
| `okxq api routes` ne lisait qu'un niveau de la table de routage | Inventaire de sécurité annonçant 6 routes là où il y en a 31 : pour un inventaire, sous-déclarer est le pire résultat (on croit avoir tout vu) | récursion dans `routes` et `original_router`, plus la liste explicite des routes dont le rôle n'est pas détectable |
| L'avis « lecture seule » de l'interface citait `HERMES_UI_MODE=full` | Instruction périmée : le droit d'agir vient du rôle porté par la clé, pas d'une variable d'environnement. Un opérateur suivant l'avis n'obtenait rien | texte corrigé en FR/EN/SQ |

Les tests correspondants échouent si l'un de ces correctifs est retiré : vérifié en les rétablissant
un par un.

## Défauts connus

- La CLI n'expose que `config`, `doctor`, `paper/shadow/demo/live` (NOT_IMPLEMENTED tant que la
  composition n'est pas livrée) ; les autres groupes apparaissent à la fusion des modules.
- `daily_spend_usd` et `cache_hit_ratio` ne sont renseignés que si le worker JEV écrit son instantané
  (`OKXQ_JEV_STATUS_PATH`) ; sans worker en marche, ils restent « non disponibles » — jamais zéro.
- **`page-labo` de Hermes est conservée dans `index.html` mais inerte** : aucune entrée de navigation
  n'y mène et aucun élément `lb-*` n'est alimenté. Le balisage et son CSS sont laissés en place parce
  que supprimer une partie de l'interface héritée est une décision de périmètre qui revient à
  l'opérateur, pas au constructeur. La vue « Recherche » couvre désormais ce besoin.
- **Collecte de marché : NOT_RUN faute d'accès réseau.** La politique de sortie de l'environnement
  refuse le CONNECT vers OKX (403, y compris vers des hôtes tiers). La dégradation observée est celle
  attendue et elle est testée : le processus continue, la santé passe `market_data` en FAULT, et la
  décision est NO_TRADE avec `DATA_STALE`.
- **29 identifiants de la matrice restent NOT_RUN** faute de test portant l'identifiant, alors que
  l'assertion correspondante existe souvent dans le code. `docs/test_matrix.md` les liste sans les
  masquer : un test non exécuté reste NOT_RUN, pas PASS.

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
