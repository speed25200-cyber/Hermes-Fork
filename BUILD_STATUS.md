# BUILD_STATUS.md — état de construction (§73)

Mis à jour à chaque incrément. Permet la reprise d'une session sans mémoire conversationnelle.
Dernière mise à jour : 18 septembre 2026 — composition livrée, PostgreSQL réellement exercé,
matrice §64 régénérée depuis un rapport JUnit.

Légende des statuts : **IMPLÉMENTÉ** (code présent, lint/mypy verts) · **TESTÉ HORS LIGNE** (tests
hermétiques exécutés et verts) · **TESTÉ SUR POSTGRESQL** (schéma et migrations exercés sur un vrai
serveur) · **BLOQUÉ : accès manquant** (nécessite clé, réseau, compte ou machine non disponibles dans
cette session) · **NON COMMENCÉ**.

Un rappel qui vaut pour tout ce document : **rien ici n'a été confronté au vrai OKX ni au vrai service
TypeSafe.** « TESTÉ HORS LIGNE » veut dire « vérifié sur fixtures », jamais « vérifié en connexion »,
et encore moins « rentable ».

## Vue par phase (§66)

| Phase | Contenu | Statut | Preuve |
|---|---|---|---|
| P0 Audit et contrats | arborescence §42, `pyproject.toml` + `uv.lock`, `Makefile`, `.env.example`, configs, `AGENTS.md`, `DECISIONS.md`, `docs/requirements_matrix.md`, `docs/architecture.md`, `docs/api_contracts.md` + les trois contrats détaillés | TESTÉ HORS LIGNE | `okxq config validate` sur 7 profils ; `tests/contract/test_api_contracts_docs.py` |
| P1 Domaine, config, sécurité initiale | `okxq.domain` (Decimal/unités, arrondis dirigés, instruments PIT, positions à coût d'ouverture exact, contrats §43, protocoles, horloges, ids), `okxq.config` (schéma strict, refus des secrets, garde LIVE par manifeste signé), CLI `config validate`/`doctor`, `live preflight|run` | TESTÉ HORS LIGNE | T01–T04, T39, T49, T64 |
| P2 Données et replay | carnet OKX (seq, checksum déprécié 2026), normalisation, qualité, archive Parquet, univers PIT, replay, REST/WS publics, capability manifest | TESTÉ HORS LIGNE | T05–T18 |
| P3 Comptabilité, simulateur, golden | ledger, PnL, funding, coûts, fills/files/latence, exchange virtuel, moteur de backtest, jeu golden §68, `okxq replay run` | TESTÉ HORS LIGNE | T23–T31, T45, `make smoke-offline` |
| P4 Recherche | features PIT (parité batch/direct), labels, splits imbriqués, baselines, calibration, stacking OOF, évaluation, registre, ablation JEV A/B/C/D, `Predictor` | TESTÉ HORS LIGNE | T19–T22 |
| P5 JEV isolé | schémas/validators, client à deadline, sources SSRF-safe, mapping PIT, cache sémantique, worker borné, corpus, `okxq jev validate-fixtures` | TESTÉ HORS LIGNE ; appel réel : BLOQUÉ : accès manquant | T50–T58 |
| P6 Portefeuille et risque | edges, covariance, optimiseur CVXPY §51, arrondis/deltas, Risk Engine, approbations liées au hash, kill switches persistés, watchdog, `okxq risk status`/`control` | TESTÉ HORS LIGNE | T40–T49, T59–T62 |
| P7 Exécution et DEMO | dépôts transactionnels, migrations Alembic, state machine, outbox, bail/fencing, gateway unique, réconciliation, protections, auth HMAC, REST/WS privés, adaptateur OKX | TESTÉ SUR POSTGRESQL ; validation DEMO connectée : BLOQUÉ : accès manquant (aucune clé DEMO) | T32–T38, T46–T47, T63 ; `tests/integration/test_postgres_schema.py` |
| P8 Interface et exploitation | API FastAPI + auth/rôles/CSRF, interface Hermes conservée et étendue (Décisions, Recherche, JEV, Risque), logs JSON masqués, métriques §61, santé, alertes, Dockerfile, `compose.yaml`, CI, sauvegardes, runbooks, threat model, ADR-001..004 | TESTÉ HORS LIGNE | T65–T69 ; smoke navigateur Chromium |
| Composition | `okxq.runtime.composition` (câblage collector/strategy/risk/gateway/jev-worker/api), `okxq paper\|shadow\|demo run`, `make smoke-offline`, `okxq backtest run`, `okxq reports export` | TESTÉ HORS LIGNE | 30 tests `tests/unit/test_composition.py` |
| P9 Dossier prospectif | collecte SHADOW, comparaison A/B/C, suivi de calibration, rapports automatiques | **NON COMMENCÉ — exige du temps réel, pas du code** | — |
| P10 Dossier LIVE | `docs/live_readiness.md` (gates NON franchis), `scripts/sign_approval_manifest.py`, garde LIVE | IMPLÉMENTÉ (mécanique) ; activation : **BLOQUÉ par conception** (aucune preuve, aucune autorisation) | T64 |
| Migration VPS | `deploy/install.sh`, `deploy/uninstall_hermes.sh`, `deploy/preflight.sh`, `deploy/etat.sh`, workflows `deploy-vps.yml` / `migrer-vers-okxq.yml` | IMPLÉMENTÉ ; exécution : **BLOQUÉ : accès manquant** (sortie réseau refusée dans cette session) | `bash -n`, YAML valides |

## Tests exécutés (dernier run)

| Commande | Résultat |
| --- | --- |
| `pytest -q -p no:randomly` (avec `OKXQ_TEST_DATABASE_URL`) | **825 passed, 0 skipped** |
| dont `tests/integration/test_postgres_schema.py` | **8 passed contre PostgreSQL 16 réel** |
| `ruff check --no-cache` / `ruff format --check` (src, tests, scripts) | 0 erreur |
| `mypy --no-incremental` (strict, `src/okxq`) | 0 erreur sur 156 modules |
| `node --test "frontend/tests/*.test.js"` | 8 passed |
| `make smoke-offline` | 21 contrôles verts, 8 exécutions (reproductibilité T70) |
| `pytest tests/e2e -m e2e` | 1 passed — interface vérifiée dans Chromium |
| `scripts/security_check.py` | AUCUNE ANOMALIE (352 fichiers) |
| `docs/test_matrix.md` | **70/70 identifiants PASS, 0 NOT_RUN, 0 FAIL** |

**Ce que « 70/70 » ne dit pas.** Chaque `PASS` signifie « ce comportement est vérifié sur fixtures hors
ligne ». La suite ne contient **aucun** test `connected` : rien n'a touché OKX ni TypeSafe. T50, T51 et
T63 sont couverts par des tests hors ligne qui exercent la dégradation, le retard et l'absence de
bascule — pas par un appel réel, qui reste impossible ici.

Les caches de lint mentent : `ruff` et `mypy` sont lancés avec `--no-cache` / `--no-incremental`
depuis qu'un `.ruff_cache` périmé a affiché « All checks passed » sur du code que la CI refusait.

## Défauts trouvés et corrigés

### Protections de risque — quatre mesures mortes en silence

| Défaut | Effet observable | Correction |
| --- | --- | --- |
| **Rien n'appelait `kill_switch.observe()`** | La frontière de jour UTC n'était jamais franchie, la perte journalière jamais calculée, le sommet d'équité jamais mis à jour : **aucun déclencheur ne pouvait se produire**. La principale protection automatique ne s'activait que sur ordre humain — donc elle n'était pas automatique | `observe_health(rt)` appelé à chaque frontière, **avant** la décision : constater le halt après aurait laissé passer exactement une décision de trop |
| **Le capital initial du compte papier n'était inscrit nulle part au grand livre** | Le grand livre est la seule source d'equity. Il démarrait à 0, donc `day_start_equity = 0`, donc `daily_loss_fraction` rendait `None` — et le Risk Engine **saute** le contrôle dont la mesure est absente. La limite de perte journalière était inatteignable en PAPER, le seul mode que la plateforme est autorisée à faire tourner | `ensure_paper_capital` inscrit une fois le capital comme flux externe observé, idempotent sur une clé **sans le montant** (une clé qui le porterait doublerait l'equity au moindre changement de configuration) |
| `RiskContext` recevait `daily_loss_fraction=None` et `drawdown_fraction=None` | Deux limites écrites, testées, et **jamais évaluées en exploitation** | les deux mesures sont demandées au kill switch, qui les calculait déjà |
| La valeur de part du runtime était une approximation locale (`equity − flux + capital initial`) | Elle rendait 1 à l'ouverture puis restait **figée à 1 pour toujours** : tout gain augmentait son dénominateur autant que son numérateur. Le drawdown surveillé était identiquement nul et son déclencheur inatteignable | `unitize` refactorisé en `next_unit_point` ; le runtime et les rapports partagent le **même** pas de calcul, et la valeur de part est persistée dans `account_snapshots` puis **lue** par le contexte de risque |
| Un dépôt externe effaçait la perte du jour | Un simple virement ramenait l'equity au-dessus de son point de départ, donc la perte journalière à zéro : il suffisait d'un transfert pour faire disparaître une limite atteinte, et rendre une reprise possible le jour même (T45) | la perte du jour est un **plafond monotone** dans le jour UTC ; seule la frontière de jour la remet à zéro |
| `ExposureSnapshot.beta()` appliquait deux scénarios globaux | Le bêta de portefeuille était sous-estimé : deux positions dont les pires cas divergent se compensaient au lieu de s'ajouter | pire cas **par instrument** |

### JEV

| Défaut | Effet observable | Correction |
| --- | --- | --- |
| Le rapprochement point-in-time acceptait une évaluation dont l'actif figurait simplement dans la cartographie du document | Sur un document multi-actifs, la cartographie porte les deux : **l'évaluation de BTC était servie comme feature d'ETH**, et réciproquement — réponses attribuées au mauvais actif, éléments probants comptés deux fois. Or `evaluate_all` fait un appel PAR actif précisément parce que les réponses diffèrent | le rapprochement porte sur `result.inst_id` seul ; le repli était par ailleurs inatteignable pour son usage supposé |
| `truncate_text(texte, n)` pouvait rendre plus de `n` caractères | Dépassement de quatre caractères — inoffensif aux budgets d'exploitation, mais la borne est ce que la fonction promet et ce sur quoi l'appelant compte pour ne pas se faire refuser une requête (§49) | coupure nette quand le budget ne peut pas porter la marque ; le drapeau signale toujours la troncature |

### Causalité, interface et exploitation

| Défaut | Effet observable | Correction |
| --- | --- | --- |
| `.gitignore` portait `data/` et `runtime/` sans ancre | `src/okxq/data/` et `src/okxq/runtime/` étaient exclus : **seize modules n'avaient jamais été versionnés** | motifs ancrés `/data/`, `/runtime/` |
| Le contrôle de causalité des prévisions avait la bonne condition et un corps vide (`pass`) | Une prévision datée avant la borne de ses propres données traversait la boucle en silence — la fuite temporelle même que la causalité doit exclure | la décision est refusée (`CAUSALITY_VIOLATION`) |
| `decision_loop` contrôlait `available_at <= cutoff_at` sur les vecteurs | Le contrat impose `available_at >= cutoff_at` : l'égalité stricte était forcée et **toute** décision aurait échoué chez un fournisseur horodatant réellement | le contrôle porte sur `cutoff_at` |
| `require_role` cherchait la piste d'audit sur `app.state.audit`, que rien ne renseignait | Un **lecteur authentifié** sondant une commande privilégiée était refusé **sans trace**, là où un anonyme en laissait une | `app.state.audit` renseigné dans `create_app` |
| `compose.yaml` lançait `okxq db upgrade head` | `head` est une option, pas un positionnel : le service de migration échouait à chaque démarrage, et les cinq rôles écrivains l'attendent. Seule l'API montait : **une console vivante devant un système mort** | `--revision head` ; `tests/unit/test_compose_commands.py` soumet chaque commande du fichier au vrai analyseur d'arguments |
| `configure_logging` remettait les handlers des loggers tiers mais pas leur **niveau** | Un niveau posé par uvicorn écartait les enregistrements avant le handler masqué : journal muet là où on le croyait filtré, masquage dépendant de l'ordre des imports | niveau remis à NOTSET ; test T66 qui échoue sans le correctif |
| `/api/v1/system/status` publiait le risque sous `halts`, `pages.js` lisait `risk` | La page Risque affichait « Non disponible » sur ses cinq lignes alors que la donnée existait | contrat verrouillé des deux côtés, sans navigateur (`tests/contract/test_ui_api_contract.py`) |
| `/api/v1/experiments` ne publiait ni `items` ni `jev_variants` ; `/api/v1/jev/status` omettait sept champs | Pages Recherche et JEV vides, sans aucune erreur | les endpoints reprennent le vocabulaire des composants ; ce qui n'existe qu'en mémoire du worker est relayé depuis son instantané, sinon `None` — jamais zéro |
| Les jeux d'entraînement sortaient 100 % `NO_ENTRY` | Le chemin de prix partait du `first_available_at` brut, décalé de 150 ms de la minute : aucun label positif ne pouvait se former | `floor_to_interval` ; `LabelSpec` refuse `horizon_s <= entry_window_s` |
| `deploy/preflight.sh` écrivait `0` quand il n'avait pas pu lire les positions | « Je n'ai pas réussi à savoir » devenait « il n'y a rien », **juste avant un effacement irréversible**. Un moteur arrêté ne ferme aucune position : elles restent ouvertes chez OKX | trois réponses distinctes (`n` / `inconnu` / échec) ; le workflow refuse sur l'inconnu sauf `positions_inconnues=accepter` |
| Trois documents de contrat référencés par `docs/api_contracts.md` n'existaient pas | L'index et la matrice des exigences (§46) pointaient dans le vide, une docstring du code y renvoyait le lecteur | les trois documents sont **générés** depuis le code et le manifeste (`scripts/render_api_contracts.py`), avec un test qui échoue si l'un dérive |

Les tests correspondants échouent si l'un de ces correctifs est retiré : vérifié en les rétablissant
un par un. Deux tests que j'avais moi-même écrits étaient creux (ils comparaient le code à lui-même) ;
`tests/unit/test_no_hollow_tests.py` interdit désormais ce motif, et l'assertion de l'en-tête
`x-simulated-trading` a été réécrite en toutes lettres après qu'une mutation l'eut traversée.

## Défauts trouvés PAR LE DÉPLOIEMENT RÉEL

Sept défauts qu'aucun test hors ligne ne pouvait voir, parce que la suite partage trois hypothèses
tacites toutes fausses en exploitation : le paquet est INSTALLÉ (pas une copie de travail), la racine
du conteneur est en LECTURE SEULE, et CINQ processus tournent sur la même base.

| Défaut | Effet observable | Correction |
| --- | --- | --- |
| Manifeste de capacités atteint par `Path(__file__).parents[4]` | Introuvable une fois le paquet installé : aucune découverte d'univers, **aucune donnée de marché**, SOFT_HALT/DATA_STALE perpétuel. La plateforme était vivante et incapable de rien | le manifeste voyage avec le paquet ; vérifié en construisant la roue |
| Alertes écrites sous `data/` | Volume en lecture seule pour la stratégie, absent pour risque/gateway/JEV : **quatre rôles sur cinq** ne pouvaient écrire aucune alerte, dont celui du risque | elles vont sous `runtime/`, un fichier par rôle |
| Les cinq rôles faisaient tourner la boucle décisionnelle | Quatre `decision` par minute sur le même compte, collisions d'unicité | seul `strategy` décide, seul `risk` conduit la protection, les autres la RELISENT (`KillSwitch.refresh`) |
| Instantané de compte réinséré à chaque frontière | Une `ERROR` PostgreSQL par minute ; un journal plein d'erreurs attendues cache celles qui ne le sont pas | on vérifie avant d'écrire ; la contrainte reste le garde-fou de course |
| `etat.sh` présentait le SECRET comme clé d'accès | `403` permanent : un diagnostic qui ressemblait à une panne d'authentification | la clé est DÉRIVÉE du secret par HMAC, comme le fait l'API |
| **Clé d'accès en clair dans un journal public** | Le journal d'uvicorn écrit l'URL complète ; recopié par un workflow, il a publié une clé admin | masquage des justificatifs en chaîne de requête, interrogation par en-tête `Authorization`, et action de rotation vérifiée |
| `docker compose restart` après avoir changé un `env_file` | Un `env_file` n'est lu qu'à la CRÉATION : **la rotation ne révoquait rien** et se déclarait réussie | `up -d --force-recreate --no-deps`, et la rotation ÉCHOUE si la nouvelle clé n'est pas acceptée |

Deux erreurs de ma part méritent d'être nommées, parce qu'elles n'étaient pas dans le code métier :

- **Une apostrophe française coupait le script distant en deux.** Les blocs envoyés à `ssh` sont des
  chaînes entre guillemets simples ; « L'échec n'est plus avalé » la refermait, et la suite tournait
  sur le runner GitHub au lieu du serveur. Le symptôme (`cd: /opt/okxq: No such file or directory`)
  était vrai, et ne désignait pas la cause. La rotation vit maintenant dans un fichier envoyé par
  l'entrée standard, et un test interdit le motif.
- **Une hypothèse fausse assumée puis corrigée** : j'ai d'abord attribué l'arrêt de l'API à un profil
  compose manquant. Vérification faite, aucun service n'en déclare. La vraie cause était `depends_on`
  — recréer `api` recréait PostgreSQL et rejouait les migrations. Le test écrit sur la fausse piste a
  été remplacé.

Le déploiement a aussi CONFIRMÉ trois correctifs du jour, en production : `capital_papier_inscrit
100000 USDT`, `day_start_equity=100000` dans l'état persisté, et le kill switch qui s'arme seul
(`halt_change NONE → SOFT_HALT, declencheurs=data_stale`). Le collecteur discute réellement avec OKX :
467 instruments retenus, 15 écartés, WebSocket public connecté.

## Pourquoi PAPER tournait sans rien faire

Sept conteneurs sains, aucune erreur dans les journaux, et la plateforme incapable de rien. Le motif
mérite d'être écrit, parce qu'aucun test unitaire ne pouvait le voir : chaque pièce fonctionnait,
c'est leur ASSEMBLAGE qui ne tenait pas.

L'état de marché (`MarketState`) vit **en mémoire**, dans le processus qui le remplit. Seul le rôle
`collector` collecte, et il n'existe **aucun transport** de sa mémoire vers celle des autres rôles.
Dans le découpage en six conteneurs, le processus qui DÉCIDE n'avait donc jamais vu une seule donnée
de marché : sa porte de données restait fermée, le kill switch passait en `SOFT_HALT` sur
`data_stale`, et chaque frontière rendait `NO_TRADE`. Indéfiniment.

PAPER tourne désormais en **un seul processus** (`--role all`, service `moteur`) : collecte,
features, décision, risque et exécution simulée partagent la même mémoire, dans le même ordre. C'est
sans danger précisément en PAPER — la plateforme n'y ouvre aucune connexion privée
(`build_exchange_adapter` rend un `VirtualExchange`), donc **aucun identifiant d'échange n'existe** :
réunir les rôles ne réunit aucun secret.

`tests/unit/test_topologie_compose.py` fixe l'invariant : tout processus qui décide doit collecter
lui-même.

## Défauts connus, non corrigés

- **DEMO et LIVE portent encore ce défaut de topologie**, et il n'est pas corrigeable en déplaçant
  des conteneurs : le gateway y détient des clés et doit rester seul (§60). Il faut un vrai transport
  de l'état de marché entre processus (message, base, mémoire partagée), qui reste à construire.
  `tests/unit/test_topologie_compose.py` le déclare en `xfail(strict=True)` : le jour où le transport
  existera, la suite échouera tant que ce fichier n'aura pas été rouvert et la correction actée.
- **Aucun modèle n'est désigné** (`OKXQ_MODEL_ARTIFACT`) : même avec des données, la boucle rend
  `NO_TRADE` avec `predictor_indisponible`. C'est voulu — §67 interdit qu'un modèle non validé pilote
  quoi que ce soit — mais cela signifie que PAPER observe et n'entre jamais tant qu'aucun artefact
  validé n'est enregistré.
- `features/derivatives.py` : `time_to_next_funding_s` peut être décalé d'un intervalle de funding
  entier au voisinage exact d'un règlement. Observé, non corrigé, non masqué.
- `place_orders_batch` n'attrape pas `ExchangeError` là où `place_order` le fait. L'asymétrie est
  sûre en l'état (l'appelant la traite), mais c'est une asymétrie.
- **`page-labo` de Hermes est conservée dans `index.html` mais inerte** : aucune entrée de navigation
  n'y mène. Supprimer une partie de l'interface héritée est une décision de périmètre qui revient à
  l'opérateur, pas au constructeur. La vue « Recherche » couvre ce besoin.
- `tests/chaos/` est un répertoire **vide** : les scénarios de panne sont couverts par des tests
  `unit` (T32, T33, T46, T47, T61, T62), pas par une campagne de chaos dédiée.
- `daily_spend_usd` et `cache_hit_ratio` ne sont renseignés que si le worker JEV écrit son instantané
  (`OKXQ_JEV_STATUS_PATH`) ; sans worker en marche ils restent « non disponibles » — jamais zéro.
- **Collecte de marché : NOT_RUN faute d'accès réseau.** La politique de sortie de l'environnement
  refuse le CONNECT vers OKX. La dégradation observée est celle attendue et elle est testée : le
  processus continue, la santé passe `market_data` en FAULT, la décision est NO_TRADE avec
  `DATA_STALE`.

## Prérequis externes (accès manquants dans cette session)

- **Clés OKX DEMO + profil de région** : validation connectée du connecteur privé (P7). Sans elles,
  aucune signature n'a été confrontée au serveur — les vecteurs de test sont construits par la
  formule documentée, ils ne prouvent pas que le serveur l'accepte.
- **Clé TypeSafe** : premier appel réel JEV (P5). La clé fournie n'a pas été utilisée : la sortie
  réseau de cette session est refusée, et un appel réel dépense le budget du détenteur.
- **Historique de marché réel** : la recherche (P4) ne dispose que de jeux synthétiques → **aucune
  preuve d'alpha**, et aucune n'est revendiquée.
- **Secrets de dépôt `VPS_PASSWORD` et `TYPESAFE_API_KEY`** dans `speed25200-cyber/Hermes` : le
  workflow de migration y est dispatchable, mais rien ne peut être déclenché depuis ici.

## Prochaine action concrète

1. Enregistrer les deux secrets de dépôt dans `speed25200-cyber/Hermes` (jamais dans le code, jamais
   dans une conversation).
2. Dispatcher `migrer-vers-okxq.yml` avec `migrer=false` : le serveur est neuf, il n'y a rien à
   effacer, et la garde `confirmer=EFFACER` ne doit pas devenir une habitude.
3. Laisser tourner en PAPER plusieurs jours pour constituer le dossier prospectif P9. C'est du temps
   réel, pas du code : aucun raccourci n'existe.
4. LIVE reste refusé par conception et le restera tant que `docs/live_readiness.md` n'a pas de
   verdict favorable adossé à des preuves.
