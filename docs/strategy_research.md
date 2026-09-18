# Recherche de stratégie : méthode, hypothèses et résultats

**Conclusion, mise en premier : aucun avantage de marché n'est démontré dans cette session.**
Aucun historique OKX réel n'a été collecté, aucune évaluation JEV réelle n'a eu lieu, aucune collecte
prospective n'a tourné. Tout ce qui a été exécuté l'a été sur des **données synthétiques seedées** et
sur des jeux **golden** déterministes, qui servent à exercer les pipelines — pas à mesurer un marché.
Le module de recherche porte cette mention dans son propre code :

```python
# src/okxq/research/__init__.py
SYNTHETIC_NOTICE = "données synthétiques — aucune preuve d'alpha"
```

Ce document décrit donc une **méthode livrée et testable**, et l'absence de résultat. C'est un
résultat valide : le développement de l'infrastructure peut être complet alors que la recherche
d'alpha n'a rien conclu.

---

## 1. Ce que l'on cherche, et ce que l'on ne cherche pas

La cible n'est pas `price_goes_up = true/false`. C'est une fonction proche de
`expected_net_edge(asset, side, horizon)` :

```text
Rendement net espéré
= rendement brut espéré
− frais espérés
− spread espéré
− slippage espéré
− coût de funding espéré
− impact de marché espéré
```

Horizons testés : 60, 300 et 900 s dans le plan d'exemple livré
(`configs/experiment.example.yaml`), avec d'autres horizons possibles. **Une minute n'est pas
présumée optimale** — c'est la cadence de décision, pas l'horizon de prédiction.

Unités, tenues par le code : `gross_mu` et les quantiles sont des **fractions de notionnel** sur
l'horizon du modèle, jamais des montants (`src/okxq/research/predictor.py`,
`src/okxq/research/training.py`). Aucun `Decimal` monétaire ne sort de la recherche : la conversion
en argent appartient au ledger. Le rendement de label lui-même est
`gross_return_on_entry_notional = s × (P1/P0 − 1)` : **ni** le rendement du capital du compte, **ni**
le rendement de marge.

---

## 2. Causalité point-in-time

C'est la partie qui décide si un résultat vaut quelque chose. Les contrôles ci-dessous sont dans le
code, pas dans une convention de nommage.

| Contrôle | Où | Ce qu'il empêche |
|---|---|---|
| Filtre `available_at <= cutoff` sur les événements bruts | `src/okxq/data/point_in_time.py` | Rejouer une décision après avoir collecté plus de données ne change pas son résultat (T14) |
| Un événement ancien reçu tard n'est disponible qu'à sa **réception** | idem | Antidater une donnée découverte plus tard (T15) |
| `assert_features_precede_decisions` | `src/okxq/research/evaluation.py`, appelé à l'extraction dans `training.py` | Une feature arrivée après la décision est une **fuite**, pas une donnée |
| `feature.available_at <= snapshot.cutoff_at` vérifié à chaque appel | `src/okxq/research/predictor.py` | Le même contrat en production qu'en recherche ; sinon `LeakageError` |
| Bougies clôturées vs intrabougie séparées explicitement (`TemporalSemantics`) | `src/okxq/features/definitions.py` | Lire une clôture avant qu'elle existe (T16) |
| Univers reconstruit point-in-time, `held_only` gardé en reduce-only | `src/okxq/data/point_in_time.py` | Le biais du survivant (T18) |
| Funding : `funding_rate_known_at(cutoff)` distingue estimation et taux réglé | `src/okxq/accounting/funding.py` | Utiliser le taux final comme feature antérieure (T26) |
| `MISSING` / `STALE` / `INVALID` / zéro observé strictement distincts, aucun remplissage arrière | `src/okxq/domain/events.py`, `features/definitions.py` | Imputer une valeur venant du futur |
| Latence **supposée** étiquetée | `src/okxq/research/synthetic.py`, `research/datasets.py` | Confondre un `available_at` mesuré et un `available_at` supposé (`LATENCY_ASSUMED`) |

Les labels portent leur propre causalité : `decision_at`, `entry_at`, `entry_window_end`,
`label_end_at`, `label_available_at`, `execution_policy_version`, `label_quality`. Un label ne
devient disponible pour un entraînement qu'après son horizon **et** la disponibilité des données
nécessaires ; `assert_label_causality` est exécuté à l'assemblage du jeu de données
(`src/okxq/research/labels.py`, `datasets.py`).

Les observations dont le résultat n'est pas observable — fin d'historique, panne, suspension — sont
**censurées** : leurs valeurs sont nulles et masquées, jamais approximées à un rendement nul
(`LabelQuality`). Les barrières ambiguës intrabougie sont marquées `AMBIGUOUS` et masquées par
défaut ; l'absence de barrière touchée est un `TIMEOUT` explicite.

Pour une politique passive, cinq grandeurs sont séparées : probabilité de fill, fraction exécutée,
rendement conditionnel au fill, coût de non-exécution et comportement à expiration. Un ordre maker
non exécuté n'est **pas** étiqueté gagnant au prix demandé
(`exec-sim-v1` dans `src/okxq/research/labels.py`).

---

## 3. Splits temporels

`src/okxq/research/splits.py` — walk-forward imbriqué, avec quatre règles appliquées par le code :

1. **Les frontières sont communes à tous les actifs.** Ce sont des dates, pas des lignes. Aucun
   `train_test_split` aléatoire n'existe dans cette API, et `assert_temporal_split` **refuse** un
   entrelacement.
2. **Purge et embargo.** `purge_s = max(purge demandé, horizon maximal + dépendances de politique)`.
   Une ligne d'entraînement dont `decision_at >= train.end − purge_s`, ou dont
   `label_available_at > next.start − embargo_s`, est purgée : elle saurait quelque chose de la
   période suivante.
3. **La provenance des transformateurs est tracée.** Normalisation, winsorisation, imputation,
   sélection de variables, PCA et calibration héritent toutes de `FittedTransformer`, enregistrent
   leur période d'ajustement, et `assert_not_fitted_on(fold.test)` lève `LeakageError` si l'une a vu
   la période de test (T17). Les hyperparamètres et les seuils ne sont choisis que sur la
   **validation** ; la période de test du fold mesure le résultat de cette sélection.
4. **La période finale est hors de tous les folds** (§ 4).

Plan d'exemple livré : 30 jours d'entraînement, 7 de validation, 7 de test, purge 900 s, embargo
900 s (`configs/experiment.example.yaml`). Ce sont des hypothèses de configuration de recherche, pas
des réglages validés.

Déterminisme : toute l'aléa vient d'une graine déclarée (`TrainingSpec.seed`,
`cfg.research.random_seed = 25200`). Même jeu de données, même graine, même spécification ⇒ mêmes
prédictions et mêmes métriques.

---

## 4. Jeu de test final gelé

`FinalTestGuard` (`splits.py`) et `ExperimentRegistry` (`src/okxq/research/experiment_registry.py`)
appliquent la règle qui protège le seul chiffre qui vaudrait quelque chose :

* la période finale (`frozen_final_test_start` → fin) n'entre dans **aucun** fold ;
* le modèle publié est réajusté uniquement sur les lignes antérieures à cette période, purge
  comprise : il ne l'a jamais vue (`training.py`, `_final_fit_indices`) ;
* `consult_final_test` inscrit `final_test_consulted_at` à la **première** consultation. À cet
  instant, la période perd son statut indépendant (T22) : une consultation transforme un test en
  information de développement ;
* ensuite, `assert_optimization_allowed` **refuse tout nouvel essai** sur cette expérience. Ce n'est
  pas un avertissement : c'est un refus ;
* chaque consultation laisse une trace consultable (rapport `final_test_consultation`) ;
* l'API ne publie `validated: true` que si le rapport porte `independent = true`
  (`src/okxq/api/routes/research.py`).

Le plan précède les résultats : `open_run` écrit l'`ExperimentPlan` (hypothèse, période, univers,
features, labels, modèle, coûts, critères de succès, budget d'essais) **et son hash** avant le premier
essai, et un essai enregistré sans run ouvert est refusé. **Tous** les essais sont journalisés, pas
seulement le gagnant : le risque de sélection multiple ne se mesure qu'avec son dénominateur
(`experiment_runs.trials`).

`cfg.research` verrouille ces exigences par typage — elles ne sont pas désactivables par
configuration : `require_temporal_oof_stacking: Literal[True]`,
`require_frozen_final_test: Literal[True]`, `require_cost_stress_tests: Literal[True]`,
`promotion_auto_approve: Literal[False]`, `max_trials_per_experiment = 30`.

---

## 5. Stacking hors-fold (OOF) temporel

`src/okxq/research/stacking.py`. Le module explique lui-même pourquoi un fold aléatoire est
inutilisable ici : sur une série temporelle, un fold tiré au hasard met du futur dans
l'entraînement de l'expert, donc le méta-modèle apprend sur des prédictions artificiellement
parfaites et le résultat hors échantillon devient inexplicable.

La construction livrée :

1. chaque expert est entraîné par `walk_forward_train` sur les **mêmes** folds chronologiques ; les
   prédictions retenues sont celles de la période de **test** de chaque fold — donc out-of-fold par
   construction ;
2. chaque ligne porte, **pour chaque composant**, la date maximale que ce composant a utilisée
   (`<expert>__max_train_at`). `assert_components_never_saw_the_row` refuse la moindre ligne dont un
   composant connaissait déjà l'instant prédit (T20). `assert_oof_is_out_of_fold` fait le contrôle
   symétrique côté `training.py` ;
3. le méta-modèle est entraîné sur les folds **antérieurs** et évalué sur le fold suivant (fenêtre
   croissante) ; `assert_temporal_split` interdit tout entrelacement ;
4. quand la cible est une probabilité, la calibration se règle sur un fold **distinct** du
   méta-entraînement et du méta-test ;
5. le méta-modèle est **verrouillé** (un seul jeu d'hyperparamètres) : ajouter une recherche à cet
   étage créerait une seconde couche de sélection que rien ne mesurerait.

Modèles disponibles (`src/okxq/research/baselines.py`) : `FlatNoTradeBenchmark` (le « ne rien
faire », étiqueté `is_benchmark` et **pas** un candidat), `RidgeModel`, `LogisticModel`,
`MomentumBaseline` et `MeanReversionBaseline` (modèles à **un** paramètre estimé sur le passé : leur
signe et leur amplitude viennent des données d'entraînement, pas d'une intuition), et
`LockedLightGBM` avec des hyperparamètres figés. Aucun **pickle** : tout se sérialise en JSON/texte,
pour qu'un artefact reste relisible, diffable et vérifiable par hash.

Calibration (`src/okxq/research/calibration.py`) : Brier, log-loss bornée, erreur de calibration
attendue (ECE), courbe de fiabilité avec effectifs **et** intervalles de Wilson, Platt et isotonique
— tous deux des `FittedTransformer` soumis à `assert_not_fitted_on`. Pour les intervalles :
couverture, largeur et perte pinball.

---

## 6. Coûts : une convention par expérience, et aucun double comptage

`src/okxq/portfolio/costs.py`. Deux conventions, choisies explicitement (`CostBasis`) :

* **MID** — le prix de référence est le milieu ; le demi-spread est un coût **explicite** ;
* **EXECUTABLE** — le prix de référence est le prix touchable ; le spread est **déjà contenu** dans
  le prix et ne doit pas être déduit une seconde fois.

`EdgeEstimate` expose `cost_basis`, `components` et `included_in_price` ; un coût déjà contenu dans
le prix et déduit à nouveau lève `DoubleCountingError` (T24). Convention par défaut du projet : MID
(décision D4).

Les composants sont **signés** : négatif = coût, positif = gain (rebate maker, funding favorable).
`slippage` est une marche déterministe dans la profondeur **observable** au-delà du premier niveau ;
`impact` est une composante **déclarée** (coefficient × √participation), nulle par défaut, pour ce que
le carnet ne montre pas — les deux ne se recouvrent pas. Les multiplicateurs de stress n'amplifient
que les coûts, jamais les gains. Contrôle imposé : à taille nulle et marché plat, sans rebate ni
funding favorable, un aller-retour **doit** coûter de l'argent.

Funding : un flux n'existe que lorsqu'un règlement est **réellement traversé** avec une position
détenue à cet instant (`cashflow = −signed_notional_at_settlement × realized_rate`) ; aucune
proratisation d'un paiement discret sur toutes les minutes (T25). Les instants de règlement sont lus
dans les événements, jamais codés en dur.

Les commissions sont normalisées en `fee_cashflow` **signé** ; `fee_cost = −fee_cashflow`. Aucun
`abs()`, qui supprimerait les rebates. Une seule source de commissions est déclarée (`FeeSource`) :
par fill **ou** agrégée par ordre, jamais les deux.

---

## 7. Mesures et statistiques

`src/okxq/research/evaluation.py`. La comptabilité de l'évaluation est volontairement simple et
déclarée :

* chaque ligne est un trade hypothétique tenu `horizon_s` : `gross = w·r_mid`,
  `cost = |w|·round_trip_cost·multiplicateur`, `net = gross − cost` ;
* agrégation par horodatage de décision, **puis division par le facteur de chevauchement**
  `horizon_s / cutoff_interval_s`. C'est ce qui empêche d'additionner des milliers de labels
  chevauchants comme s'il s'agissait d'opérations toutes finançables et indépendantes ;
* courbe d'equity additive, drawdown maximal sur cette courbe, turnover, coûts détaillés,
  concentration par actif et par jour (Herfindahl) ;
* **Sharpe** : uniquement sur rendements agrégés par **jour UTC**, annualisé par `√365`, et seulement
  s'il y a au moins **10 jours** — sinon `None` **avec sa raison** écrite dans les notes. Aucune
  annualisation d'un Sharpe à la minute ;
* **bootstrap temporel par blocs** sur la matrice (horodatages × instruments) : les **mêmes** blocs
  s'appliquent à tous les actifs, ce qui conserve leur dépendance. Longueurs de blocs testées :
  12, 30, 60, avec publication de la sensibilité. Un intervalle de confiance n'est pas une garantie
  de stabilité future.

### Contrôles négatifs et benchmarks

| Contrôle | Fonction | Ce qu'il détecte |
|---|---|---|
| Labels mélangés | `shuffled_label_control` | Un pipeline contaminé reste « prédictif » après mélange : c'est le signe d'une fuite (seuil IC 0,1) |
| Features décalées vers le futur | `future_feature_screen` | Structurellement (`available_at > decision_at`) **et** statistiquement (IC implausible ≥ 0,5) |
| Coûts majorés | `cost_stress` | Un avantage qui disparaît dès que les coûts montent |
| Délais dégradés | `latency_stress` | Une dépendance à une latence optimiste |
| Benchmarks | `benchmark_suite` | `flat` (ne rien faire), buy-and-hold, signe du momentum, **signe aléatoire à turnover égal** |

Le benchmark « signe aléatoire à turnover égal » est le plus sévère : il paie les mêmes coûts et ne
sait rien.

Critère de succès déclaré dans le plan d'exemple :
`net_pnl_bootstrap_lower_bound_positive` — la borne **inférieure** d'un intervalle bootstrap net doit
être positive sur une période réservée. Les critères de promotion sont définis **avant** l'observation
finale, et `promotion_auto_approve` est typé `Literal[False]`.

---

## 8. Ablation JEV : A, B, C, D

Le protocole exigé, et son état réel dans le dépôt :

```text
A : modèles quantitatifs seuls
B : A + métadonnées d'événements, SANS JEV
C : A + métadonnées + variables sémantiques JEV
D : C avec questions/features retirées une à une par groupes
```

La variante **B est le point essentiel** : sans elle, on attribue à JEV un gain qui vient de la
simple **présence d'une annonce**. Les fenêtres, budgets de recherche et règles de coûts doivent être
comparables — régler C dix fois plus que A puis présenter la différence comme un effet causal isolé
n'est pas une mesure.

État livré — `src/okxq/research/jev_ablation.py` construit et exécute les quatre variantes, avec la
définition exacte suivante :

```text
A : modèles quantitatifs seuls
B : A + métadonnées d'événements (présence, nombre, âge) — AUCUNE sémantique
C : B + variables sémantiques JEV
D : C avec un groupe de questions JEV retiré à la fois
```

Les règles d'équité sont imposées **par le code**, pas par la bonne volonté :

* toutes les variantes partagent le **même** jeu de données — mêmes lignes, mêmes labels, mêmes
  coûts. Seules les colonnes de features changent (`variant_feature_sets`) ;
* toutes partagent la **même** `TrainingSpec` : mêmes folds, même graine, même budget d'essais, même
  politique de signal, même multiplicateur de coûts. On ne règle pas C dix fois plus que A ;
* `net_pnl` vient du **même** simulateur pour tout le monde (`evaluate_oof`, frais et spread inclus),
  et c'est un rendement **cumulé en fraction de notionnel** par intervalle de décision — pas un
  montant ;
* `assert_jev_groups_cover_registry` vérifie que les groupes de questions correspondent bien au jeu
  de questions déclaré ;
* une variante mesurée sur une période **déjà consultée** porte `independent=False` : ce n'est plus
  une preuve, c'est de l'information de développement ;
* la conclusion `jev_adds_net_utility` vaut **`None` quand la question ne peut pas être tranchée**.
  Elle n'est jamais forcée à `True` pour pouvoir annoncer « IA avec JEV ».

Détail important, et c'est la garantie anti-triche du module : sur données synthétiques, les
probabilités JEV produites par `synthetic_jev_evaluations` sont du **bruit seedé, indépendant du
futur** — précisément pour que C ne puisse pas « gagner » par construction. Chaque rapport porte
l'avertissement explicite qu'aucun avantage de marché n'est mesuré.

Le reste de la chaîne : `ExperimentRegistry.KIND_JEV_ABLATION = "jev_ablation"` stocke les rapports
dans `evaluation_reports` ; `src/okxq/api/routes/research.py` les agrège en `jev_variants` (variante,
PnL net, début de période, indépendance) ; le panneau « Apport de JEV : A, B, C, D » de l'interface
les affiche (`docs/ui.md`) ; et `tests/contract/test_ui_api_contract.py` vérifie qu'une ligne
d'ablation porte bien `variant`, `net_pnl`, `period_start` et `independent`.

**Ce qui n'existe pas** : **aucune évaluation JEV réelle n'a eu lieu** (aucune clé TypeSafe). Les
variantes ne peuvent donc être exécutées que sur des évaluations **synthétiques**, c'est-à-dire du
bruit. Aucun résultat d'ablation produit dans cette session ne dit quoi que ce soit de l'apport réel
de JEV : seule une évaluation prospective, sur des documents réels et avec les sorties archivées
avant résultat, le dirait.

Règles qui s'appliqueront dès qu'un résultat existera :

* la qualité sémantique (sur corpus annoté), la calibration du modèle financier aval et la performance
  nette du portefeuille sont évaluées **séparément**. Le corpus livré est
  `tests/fixtures/jev/corpus.jsonl` ;
* JEV n'est **jamais** juge de sa propre qualité ;
* une découverte automatique de questions est de la **recherche d'hyperparamètres** : elle reste
  limitée à l'entraînement et au budget d'essais ;
* des probabilités JEV utilisables par un modèle aval ne prouvent **pas** un alpha de marché ;
* un changement de version JEV, de gabarit, de mapping ou de nettoyage de texte crée une **nouvelle
  version de features** et impose une revalidation — c'est exactement ce que porte la clé de cache à
  cinq composantes (`docs/data_dictionary.md` § 3.2) ;
* si JEV n'améliore pas significativement l'utilité nette ou la protection, il reste en **shadow** ou
  son influence est désactivée. L'intégration et les rapports sont conservés ; aucun poids non nul
  n'est forcé pour pouvoir annoncer « IA avec JEV ».

---

## 9. Données disponibles : synthétiques et golden

| Source | Ce que c'est | Ce que ça prouve |
|---|---|---|
| `src/okxq/research/synthetic.py` | Marche aléatoire log-normale à facteur commun, seedée, **sans alpha planté par défaut**, au format de `docs/event_schemas.md` | Que les pipelines tournent et sont reproductibles. **Rien** sur le marché |
| `planted_momentum` | Signal **connu** injecté dans un test | Contrôle **positif** : le pipeline sait retrouver un signal qui existe. Jamais pour produire un résultat présentable |
| `tests/fixtures/golden/` et `golden_regimes/{favorable,defavorable,plat}` | Jeux déterministes avec `manifest.json` (checksum SHA-256, `quality_level`, bornes de disponibilité) | Reproductibilité d'un parcours hors ligne (T69) |

Niveaux de qualité déclarés (`research/datasets.py`) : **A** = carnet + trades horodatés par
l'exchange ; **B** = bougies seules ; **C** = synthétique ou incomplet. Tout ce qui a été exécuté ici
est de niveau **C**, avec le drapeau `LATENCY_ASSUMED` : les `available_at` sont supposés, pas
mesurés.

`dataset_hash` (SHA-256 du contenu canonique + hash du schéma de features + spécifications) lie un
résultat à ses données exactes. Le facteur de chevauchement `overlap[h] = h / cutoff_interval_s` est
calculé et pris en compte dans le nombre effectif d'observations.

**Aucun historique L2 réel n'existe.** Le cahier des charges est explicite sur la conduite à tenir :
construire d'abord la collecte et les baselines compatibles avec ce qui existe, et **ne pas simuler
des mois de microstructure de précision fictive**. C'est ce qui a été fait.

---

## 10. Ce qui a réellement été exécuté, et ce qui reste NOT_RUN

### Exécuté

* La suite hermétique : `pytest -m "not integration and not connected"`, avec rapport JUnit dans
  `reports/junit.xml`, dont le smoke d'interface dans un vrai Chromium. Les compteurs exacts et le
  détail par exigence §64 sont dans **`docs/test_matrix.md`**, qui est **généré** depuis ce rapport
  par `scripts/test_matrix_status.py` : c'est ce fichier qui fait foi, pas une phrase recopiée ici.
  Au moment de sa dernière génération : 40 exigences en `PASS`, 30 en `NOT_RUN`, 0 en `FAIL` — mais
  **la suite n'était pas verte pour autant** : elle comportait 2 cas en échec qui ne nomment aucun
  identifiant §64 (les tests du script de contrôle de sécurité), et la matrice le signale
  explicitement plutôt que de laisser croire à un état de santé global.
* Le parcours hors ligne `make smoke-offline`, dont l'artefact `reports/smoke-offline.json` est
  présent dans le dépôt (produit par un incrément antérieur de la construction, **pas** par la
  rédaction de ce document) : 21 contrôles verts, 4 régimes (`neutre`, `favorable`, `defavorable`,
  `plat`) × 2 scénarios (`flat`, `scripted`), 766 événements appliqués et 5 frontières de décision
  par exécution, plus le contrôle `reproductibilite_T70`.

  Ce que ce rapport montre, et c'est exactement ce qu'il faut en retenir :

  | Régime | Scénario | Fills | PnL de stratégie | Frais cumulés |
  |---|---|---|---|---|
  | neutre | flat | 0 | `0.00000000` | `0` |
  | neutre | scripted | 4 | `-0.38800460` | `-0.38500460` |
  | favorable | scripted | 2 | `-0.23513250` | `-0.32013250` |
  | defavorable | scripted | 2 | `-0.28905125` | `-0.19245125` |
  | plat | scripted | 4 | `-0.38720000` | `-0.38500000` |

  Le PnL de stratégie est **négatif dans tous les scénarios avec exécution**, essentiellement du
  montant des frais, et strictement nul quand aucun ordre n'est passé. Les invariants comptables
  tiennent (`marked_equity` = `liquidated_equity`, exposition résiduelle nulle). C'est un résultat de
  **plomberie** : il montre qu'un aller-retour coûte de l'argent et que la comptabilité est
  conservative. Le rapport porte lui-même son avertissement : « Jeux de données SYNTHÉTIQUES et
  scénarios de TEST : ce parcours prouve que la chaîne fonctionne et que la comptabilité tient, pas
  qu'une stratégie est rentable. » **Aucun de ces chiffres n'est une mesure de marché, et le régime
  nommé « favorable » n'est favorable que par construction du scénario de test.**

### NOT_RUN — et bloquant pour la capacité correspondante

| Ce qui manque | Pourquoi |
|---|---|
| Tout historique de marché réel | aucune collecte, aucune clé, aucun accès réseau d'exchange dans cette session |
| Toute évaluation JEV réelle | aucune clé TypeSafe |
| Collecte prospective (SHADOW) | jamais démarrée |
| **T17** (ajustement sur le test), **T19** (labels chevauchants), **T20** (stacking OOF), **T21** (label censuré), **T22** (consultation du test final) | les assertions existent dans le code, mais **aucun test ne porte ces identifiants** — voir `docs/test_matrix.md` |
| **T23**–**T26** (frais, double comptage, funding traversé, fuite de funding) | idem : implémenté, non testé sous son identifiant |
| **T40**, **T41** (arrondi cassant la neutralité, solveur en échec) | idem |
| **T70** (même dataset/config/seed ⇒ même résultat) | le contrôle de reproductibilité existe dans `smoke_offline.py` (`reproductibilite_T70`, vert), mais aucun test pytest ne porte l'identifiant → la ligne reste `NOT_RUN` dans la matrice |
| Une ablation A/B/C/D sur des évaluations JEV **réelles** | l'exécuteur existe (§ 8), mais sans clé TypeSafe il ne peut tourner que sur du bruit synthétique |

### Les trois gates, honnêtement

* **Gate technique** : partiellement franchissable hors ligne ; le connecteur DEMO, la réconciliation
  réelle, les protections côté exchange et la restauration de sauvegarde sont NOT_RUN.
* **Gate scientifique** : **non franchi**, et il ne peut pas l'être. Il n'y a ni historique réel, ni
  période indépendante, ni PnL net mesuré, ni collecte prospective. Aucune borne inférieure de
  bootstrap positive n'a été obtenue, parce qu'aucune n'a été calculée sur des données réelles.
* **Gate opérateur et compte** : à renseigner par l'opérateur ; aucun manifeste d'approbation
  n'existe.

Voir `docs/live_readiness.md` pour le détail des gates.

---

## 11. Engagements de méthode

* **Un mauvais résultat est un résultat valide.** Aucune recherche illimitée jusqu'à découvrir
  artificiellement un backtest positif. Le budget d'essais est borné
  (`max_trials_per_experiment = 30`, `max_job_minutes`, `max_cpu_threads`), l'exploration s'arrête à
  son budget, conserve les essais et rend un rapport **même sans modèle gagnant**.
* **Aucun seuil ne sera baissé après coup** pour franchir un gate. Les critères de promotion sont
  enregistrés avant l'observation finale.
* **Aucun chiffre présenté sans sa convention.** Un pourcentage de rentabilité sur une durée
  s'accompagne du capital et de la convention utilisés ; un Sharpe s'accompagne de sa fréquence, de
  sa convention d'annualisation et du traitement de l'autocorrélation.
* **Les graphiques sont générés à partir des résultats enregistrés**, jamais retouchés.
* **Aucune de ces phrases n'est une promesse de rendement.** Ni rendement garanti, ni zéro risque, ni
  stop garanti, ni neutralité parfaite, ni reproduction exacte d'une file L2 : le cahier des charges
  interdit ces annonces, et rien dans ce dépôt ne les soutiendrait.
* **Tant que l'échantillon est insuffisant, le système reste en SHADOW/PAPER.** C'est l'état actuel.
