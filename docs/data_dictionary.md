# Dictionnaire de données

Portée : les **30 tables** de `src/okxq/persistence/models.py` et les **contrats** de
`src/okxq/domain/events.py`, tels qu'ils existent dans le dépôt. Pour chaque champ : ce qu'il veut
dire, son unité, et s'il **fait autorité** ou s'il est **dérivé** (affichage seulement).

Ce document décrit le schéma livré. Il ne décrit aucune donnée de marché réelle : aucun historique
OKX n'a été collecté dans cette session, et les seules lignes jamais écrites dans ces tables l'ont été
par des tests et par les jeux de données synthétiques ou golden.

---

## 1. Conventions transversales

| Convention | Règle appliquée dans le code | Fichier |
|---|---|---|
| Fuseau | Toute date stockée est **UTC timezone-aware**. `TZDateTime` refuse une date naïve à l'écriture et rend toujours une date UTC en lecture. L'affichage peut être `Europe/Zurich` ; aucun calcul ne dépend d'une date locale. | `src/okxq/persistence/types.py`, `src/okxq/domain/clocks.py` |
| Deux horloges | `Clock.now_utc()` pour les **instants**, `Clock.monotonic_ns()` pour les **durées**. Une horloge monotone ne se compare ni entre machines ni entre redémarrages : `HOST_ID` et `BOOT_ID` accompagnent ce qui en dépend. | `src/okxq/domain/clocks.py` |
| Argent | `Decimal` partout, jamais de `float`. `dec()` refuse un `float`, un booléen, un NaN et un infini ; `dec_from_float(x, places)` est la seule conversion, et elle déclare sa quantification. | `src/okxq/domain/money.py` |
| Précisions | Montants et notionnels `NUMERIC(28,8)` (`MoneyNumeric`) ; prix et quantités `NUMERIC(28,12)` (`PriceNumeric`, `QtyNumeric`) ; fractions `NUMERIC(18,12)` (`FractionNumeric`). `check_numeric_bounds()` refuse une valeur que la colonne ne peut pas représenter. | `src/okxq/persistence/types.py` |
| Sérialisation | Les décimaux monétaires sortent en **chaînes** ; les probabilités et sorties statistiques sont des flottants **finis** validés (`FiniteFloat`). | `src/okxq/domain/events.py` |
| Absence | `MISSING`, `STALE`, `INVALID` et « zéro observé » sont quatre états distincts (`QualityFlag`). Une valeur absente vaut `None` avec son masque — **jamais** 0. `FeatureVector` refuse une valeur présente sous un masque non-`OK`, et refuse un masque `OK` sans valeur. | `src/okxq/domain/events.py`, `src/okxq/features/definitions.py` |
| Signes | `fee_cashflow` est **signé** : négatif = débit, positif = crédit (rebate). Aucun `abs()`, qui supprimerait les rebates. Quantités de position signées : `> 0` long, `< 0` short. | `src/okxq/accounting/ledger.py`, `src/okxq/domain/money.py` |
| Immuabilité | Tous les contrats héritent de `Contract` : `frozen=True`, `extra="forbid"`. Un champ inconnu est refusé, pas ignoré. | `src/okxq/domain/events.py` |

### Dérivé ou faisant autorité

La distinction n'est pas cosmétique : une valeur **dérivée** recalculée peut différer au douzième
chiffre, et l'utiliser comme base d'un calcul comptable fabrique une erreur qui grossit.

| Fait autorité | Dérivé (affichage / lecture) |
|---|---|
| `positions.open_cost` — somme signée exacte des notionnels d'entrée encore ouverts | `average_entry_price` = valeur absolue de `open_cost / signed_base_qty` |
| `ledger_entries.amount` — écritures équilibrées par devise | `account_snapshots.equity`, tout PnL agrégé |
| `fills.fee_cashflow` par exécution | totaux de frais du fournisseur (contrôles, pas événements) |
| événements observés (`order_events`, `fills`) | `orders.observed_state`, `orders.cumulative_filled` (projection) |
| `risk_state` persisté | `halt_state` affiché |

**Le prix d'entrée moyen est dérivé du coût ouvert, jamais l'inverse.**
`okxq.domain.positions.Position` stocke `open_cost` et calcule `average_entry_price` dans
`__post_init__` ; `unrealized_pnl(mark)` vaut `signed_base_qty × mark − open_cost`. Reconstruire
`open_cost` depuis un prix moyen arrondi casserait l'identité
« flux de trésorerie + valeur marquée = PnL réalisé + PnL latent ». Sur une fermeture partielle, le
coût libéré est proratisé et quantifié à 12 décimales ; sur une fermeture totale, le coût libéré est
**exactement** `open_cost`, de sorte que l'erreur de prorata (≤ 1e-12) est réalisée exactement à la
clôture. (`src/okxq/domain/positions.py`)

---

## 2. Causalité point-in-time : les quatre champs qui décident

Ces champs ne sont pas des métadonnées d'audit : ce sont eux qui déterminent **ce que le système
savait**. S'en écarter fabrique du look-ahead sans qu'aucun test de type ne s'en aperçoive.

| Champ | Sens exact | Unité | Autorité |
|---|---|---|---|
| `available_at` | Instant à partir duquel la donnée est **utilisable** par une décision : après réception, validation et publication. Validé `>= receive_ts` sur `EventEnvelope`. Un document publié hier mais découvert aujourd'hui n'était **pas** connu hier. | timestamptz UTC | fait autorité pour toute lecture |
| `cutoff_at` | Instant de coupe d'un snapshot décisionnel : la frontière de ce qui est lisible. `PointInTimeStore` filtre `available_at <= cutoff` — c'est ce filtre, et non un tri par `exchange_ts`, qui rend une décision rejouable à l'identique. | timestamptz UTC | fait autorité |
| `started_at` | Début du calcul de la décision. `DecisionLoop.run_once` lève `CausalityError` si `started_at < cutoff_at`. | timestamptz UTC | fait autorité |
| `ingest_seq` | Numéro d'ingestion, strictement croissant par source. Sert de **départage stable** entre événements à horodatage égal : le replay ordonne par `(available_at, ingest_seq)`. Ce n'est pas une causalité inter-canaux, seulement l'ordre de réception enregistré. | entier ≥ 0 | fait autorité pour l'ordre |

Chaîne imposée par le cahier des charges (§34) et son état réel dans le code :

```text
feature.available_at <= snapshot.cutoff_at <= decision.started_at   → vérifié
forecast.available_at <= portfolio.started_at                       → partiellement
approval.created_at <= order.sent_at <= approval.expires_at         → vérifié
```

* `DecisionLoop` refuse un snapshot dont une feature a `available_at > cutoff_at`, refuse
  `started_at < cutoff_at`, et refuse une prévision issue d'un autre `snapshot_id`.
* `ApprovedOrder` lie l'ordre au hash exact du payload approuvé ; `recheck_at_send`
  (`src/okxq/risk/approvals.py`) refuse une approbation expirée (T48) ou un payload modifié (T49).
* **Limite à connaître** : le validateur de `FeatureVector` exige `available_at >= cutoff_at`, tandis
  que `DecisionLoop` exige `available_at <= cutoff_at`. Les deux ensemble forcent l'égalité, donc le
  champ ne porte aujourd'hui aucune information supplémentaire au niveau du vecteur : la vraie
  causalité est portée par le filtre `available_at <= cutoff` sur les **événements bruts**
  (`src/okxq/data/point_in_time.py`). Le contrôle correspondant sur `Forecast` dans
  `decision_loop.py` est une branche sans effet (`pass`) : la borne n'est donc pas imposée sur les
  prévisions.

### Horodatages d'un document (JEV)

`document_versions` et `JevEvaluation` conservent la chaîne complète, parce qu'une feature sémantique
ne devient utilisable qu'après **calcul et publication**, jamais à l'heure déclarée de publication :

`published_at` (déclaré, peut être `NULL`) → `first_seen_at` → `received_at` → `parsed_at` →
`inference_completed_at` → `features_committed_at`.

`JevEvaluation` refuse toute antidatation : chaque instant doit suivre le précédent, et un statut
`ok` sans `features_committed_at` est rejeté (T51). Une date manquante reste manquante :
`date_method="absent"` et `published_at = NULL` — l'heure de collecte n'est **jamais** substituée
(T58).

---

## 3. Tables

30 tables, créées par `Base.metadata` et par la migration `0001_initial`
(`src/okxq/persistence/migrations/versions/`). Le cahier des charges §44 en énumère 29 ; `risk_state`
est ajoutée pour rendre le halt et la perte journalière **persistants** à travers un redémarrage
(T44).

### 3.1 Marché et métadonnées

#### `instrument_versions` — métadonnées d'instrument, versionnées

| Champ | Sens | Unité | Autorité |
|---|---|---|---|
| `inst_id`, `version` | Instrument et numéro de version ; unique ensemble | — | fait autorité |
| `valid_from` | Début de validité **économique** de cette version | timestamptz | fait autorité |
| `observed_at` | Instant où **nous** avons observé cette version | timestamptz | fait autorité (point-in-time) |
| `settle_ccy`, `base_ccy` | Devise de règlement (`USDT` seule acceptée) et devise de base | code devise | fait autorité |
| `contract_type` | `linear` seul accepté ; inverse/option/échéance refusés avant modèle et avant ordre (T02) | énum | fait autorité |
| `base_units_per_contract` | `v` : unités de base par contrat, dérivé de `ctVal` **uniquement** si `ctType=linear`, `ctValCcy=base` et `ctMult=1` ; sinon instrument non supporté | unités de base / contrat | fait autorité |
| `tick_size`, `lot_size`, `min_size` | Grilles de prix et de taille ; contrainte `> 0` | prix / contrats | fait autorité |
| `state` | État publié (négociable, suspendu, délisté…) | énum | fait autorité |
| `max_leverage` | Levier maximal publié, `NULL` si inconnu | multiple | informatif |
| `provenance`, `raw` | Origine de la métadonnée et charge brute conservée | texte / JSON | fait autorité (audit) |

Lire la version à sa **date d'observation** est ce qui empêche de réinterpréter le passé avec une
métadonnée modifiée en cours de session (T04).

#### `universe_snapshots` — univers admissible daté

`universe_version` (PK), `computed_at`, `valid_from`, `eligible` (liste d'`inst_id` admissibles),
`held_only` (hors univers mais encore en portefeuille → **reduce-only**, jamais effacé de la
comptabilité, T18), `criteria` (filtres appliqués), `bias_note` (biais explicité quand la
reconstruction point-in-time est impossible). Fait autorité.

#### `raw_partitions` — manifeste des partitions Parquet immuables

`dataset`, `inst_id`, `partition_key`, `path`, `checksum_sha256`, `rows`, `first_ts`, `last_ts`,
`written_at`, `schema_version`. Les données brutes volumineuses ne vivent pas en base : la table est
le **manifeste** qui permet de vérifier l'intégrité d'un fichier avant de le rejouer. Le checksum
fait autorité.

#### `data_quality_events` — incidents de données

`occurred_at`, `inst_id`, `channel`, `kind`, `severity`, `details`. C'est le compteur qui empêche de
masquer une perte : un trou de flux rend les features concernées invalides et laisse une trace.

### 3.2 Documents et composant sémantique JEV

#### `source_documents` — identité d'un document

`document_id` (PK), `source`, `source_url`, `deduplication_id` (unique avec `source`),
`first_seen_at`, `language`. L'identité est `(source, deduplication_id)` : deux événements distincts
portant un même titre ne sont pas fusionnés.

#### `document_versions` — versions successives, sans réécriture

| Champ | Sens | Autorité |
|---|---|---|
| `document_id`, `version` | Unique ensemble ; un changement de contenu **crée une version**, sans effacer la précédente | fait autorité |
| `published_at` | Date de publication **déclarée**, `NULL` si absente | déclaratif, jamais substitué |
| `date_method` | Comment la date a été obtenue : `page_metadata`, `declared_field`, `rss_pubdate`, `absent` | fait autorité |
| `received_at`, `parsed_at` | Réception effective, puis analyse | fait autorité |
| `raw_text_hash` | Empreinte du texte brut autorisé | fait autorité |
| `title`, `text` | Contenu — **donnée, jamais instruction** (T55) | données |
| `asset_mapping`, `asset_mapping_version` | Candidats actif ↔ document et version du registre de correspondance | fait autorité |

#### `jev_requests` / `jev_results` — appels et cache sémantique

`jev_requests` : `request_id`, `document_version_id`, `model_version`, `question_hash`,
`asset_mapping_version`, `requested_at`, `deadline_at` (deadline **murale** totale),
`payload_hash`, `status`, `attempts`.

`jev_results` : `evaluation_id`, `request_id`, `document_version_id`, `model_version`,
`model_effective` (version **retournée**, distincte de celle demandée), `question_hash`,
`asset_mapping_version`, `cleaning_pipeline_version`, `requested_at`, `completed_at`,
`inference_completed_at`, `features_committed_at`, `status`, `answers`, `usage` (consommation
facturée lue dans la réponse), `error`, `inst_id`.

Clé du cache sémantique — unique sur
`(model_version, question_hash, document_version_id, asset_mapping_version, cleaning_pipeline_version)`.
Changer l'une de ces cinq composantes crée une **nouvelle** entrée : c'est ce qui empêche de servir
une réponse obtenue avec un autre modèle, un autre gabarit de questions, un autre mapping ou un autre
nettoyage de texte. **Un cache ne réinitialise pas l'âge d'un événement** : `first_seen_at` et
`published_at` restent ceux du document d'origine.

### 3.3 Recherche et modèles

| Table | Champs porteurs de sens | Notes |
|---|---|---|
| `feature_schemas` | `schema_hash` (PK), `version`, `names`, `definitions`, `created_at` | Le hash lie un modèle au **jeu exact** de features et à leurs définitions (unité, sémantique temporelle, politique d'absence) |
| `model_versions` | `model_id`, `family`, `status`, `code_commit`, `dataset_hash`, `feature_schema_hash`, `period_start/end`, `universe_version`, `manifest`, `artifact_path`, `artifact_sha256`, `created_at`, `promoted_at`, `promoted_by` | `artifact_sha256` fait autorité : un artefact déployé vient d'un registre contrôlé avec hash. `promoted_at/by` tracent qui a promu, pas seulement quoi |
| `experiment_runs` | `run_id`, `plan_id`, `plan_hash`, `plan`, `started_at`, `finished_at`, `status`, `trials`, `final_test_consulted_at`, `code_commit`, `seed` | `trials` conserve **tous** les essais, pas le gagnant seul. `final_test_consulted_at` est la date à laquelle la période réservée a été consultée : après cette date elle n'est plus une période indépendante (T22) |
| `evaluation_reports` | `report_id`, `run_id`, `model_id`, `kind`, `period_start/end`, `independent`, `metrics`, `created_at` | `independent` fait autorité : l'API ne publie `validated: true` que si `independent` est vrai (`src/okxq/api/routes/research.py`) |

### 3.4 Décisions, portefeuille, intentions, approbations

#### `decisions` — une ligne par frontière de décision, **NO_TRADE compris**

`decision_id`, `mode`, `snapshot_id`, `cutoff_at`, `started_at`, `finished_at`,
`outcome` (`TRADE` / `NO_TRADE` / `SKIPPED` / `FAILED`), `reason_codes`, `model_id`,
`universe_version`, `equity_version`, `inputs_hash`, `forecasts`, `edges`,
`rejected_alternatives`, `timings_ms`, `software_version`, `trace_id`.

Une absence de trade est une décision valide et elle est **enregistrée avec ses raisons et ses
alternatives rejetées** : sans cela, on ne peut ni auditer ni rejouer ce qui n'a pas eu lieu.

#### `portfolio_targets`

`target_id`, `decision_id`, `snapshot_id`, `equity_version`, `signed_weights` (poids signés, fraction
de l'équité), `constraints_version`, `solver_status`, `solver_report`, `created_at`, `expires_at`.
Un `solver_status` non optimal n'autorise aucune stratégie de secours non validée.

#### `order_intents` — ce que le modèle **propose**

`intent_id`, `decision_id`, `target_id`, `account_scope`, `inst_id`, `side`,
`contracts` (> 0, en **contrats**), `price_limit` (prix), `order_type`, `reduce_only`, `ttl_ms`
(> 0), `reason`, `client_order_id`, `payload_hash`, `created_at`, `expires_at`.
Unicité durable `(account_scope, client_order_id)`.

#### `risk_approvals` — ce que le Risk Engine **autorise**

`approval_id`, `intent_id`, `intent_hash`, `action` (`ALLOW` / `REDUCE` / `REJECT` / `FLATTEN`),
`allowed_payload_hash`, `allowed_contracts`, `limits_version`, `position_version`, `reservations`,
`reason_codes`, `created_at`, `expires_at`.

`allowed_payload_hash` fait autorité : toute modification de prix ou de taille au-delà de ce qui a
été approuvé change le hash et impose une **nouvelle** validation. `limits_version` et
`position_version` datent le contexte : une approbation n'est pas valable dans un autre contexte.

### 3.5 Ordres, événements, fills, réservations

#### `orders` — projection de l'état **observé**

`order_id`, `account_scope`, `client_order_id` (unique par `account_scope`), `intent_id`,
`approval_id`, `exchange_order_id`, `inst_id`, `side`, `order_type`, `contracts`, `price_limit`,
`reduce_only`, `observed_state`, `pending_operation`, `cumulative_filled`, `average_fill_price`,
`payload_hash`, `sent_payload` (payload **exact** envoyé), `attempt_count`, `created_at`, `sent_at`,
`ack_at`, `terminal_at`, `updated_at`, `version`.

* `observed_state` est ce que l'exchange a **montré**, pas ce que nous espérions. `UNKNOWN` est un
  état légitime : réponse perdue après envoi, réservation maintenue, aucun retry aveugle (T32/T33).
* `cumulative_filled` et `average_fill_price` sont **dérivés** des fills ; les fills font autorité.
* `version` est un compteur optimiste : aucune mise à jour aveugle n'écrase un fill tardif.

#### `order_events`

`event_id`, `order_id`, `account_scope`, `client_order_id`, `exchange_order_id`, `event_kind`,
`observed_state`, `cumulative_filled`, `event_ts` (horodatage exchange, nullable),
`receive_ts` (réception locale), `raw_hash`, `raw`. Déduplication sur
`(account_scope, client_order_id, raw_hash)` : un message répété n'a aucun second effet (T34).

#### `fills` — la source d'autorité de la comptabilité

| Champ | Sens | Unité |
|---|---|---|
| `execution_key` (PK) | Clé de déduplication construite selon la **portée réelle** des identifiants ; `trade_id` n'est pas présumé globalement unique | texte |
| `contracts` | Quantité exécutée, `> 0` (le sens est porté par `side`) | contrats |
| `fill_price` | Prix d'exécution, `> 0` | prix quote |
| `fee_cashflow` | Commission **signée** : négatif = débit, positif = rebate | devise `fee_ccy` |
| `liquidity` | `maker` / `taker` / `unknown` — jamais deviné | énum |
| `fill_at` / `receive_ts` | Instant d'exécution / instant de réception | timestamptz |

#### `execution_reservations` — exposition réservée, pessimiste

`reservation_id`, `account_scope`, `intent_id`, `inst_id`, `signed_contracts`, `notional_usdt`,
`pessimistic` (défaut vrai), `status` (`ACTIVE` / `RELEASED`), `created_at`, `released_at`,
`release_reason`. Une réservation n'est libérée que sur un état final **observé** — jamais à la
simple demande d'annulation. La libération est monotone.

### 3.6 Comptabilité

#### `ledger_transactions` / `ledger_entries` — journal append-only

`ledger_transactions` : `txn_id`, `account_scope`, `kind`, `idempotency_key` (unique par
`account_scope`), `fill_key`, `occurred_at` (instant **économique**), `recorded_at` (instant
d'écriture), `description`, `metadata`.

`ledger_entries` : `txn_id`, `account` (ex. `cash:USDT`, `fees:USDT`, `pnl:realized`), `ccy`,
`amount` (signé), `inst_id`.

La somme des écritures d'une transaction est **nulle par devise**. `cash:<ccy>` porte le flux de
trésorerie réel, les comptes de contrepartie portent l'opposé ; la contribution d'un compte se lit
donc directement comme `-solde`. L'idempotence par `idempotency_key` garantit qu'un fill livré deux
fois n'a aucun second effet.

#### `account_snapshots`

`account_scope`, `equity_version` (unique ensemble), `as_of`, `source` (`ledger` ou `exchange` —
deux vérités distinctes, rapprochées par fenêtres de réconciliation, jamais par égalité
instantanée), `cash_collateral`, `unrealized_pnl`, `equity`, `available_margin`, `used_margin`,
`external_cashflow_cum`, `unit_value`, `raw`.

* `equity = cash_collateral + unrealized_pnl + autres actifs supportés` — **dérivé**.
* `unit_value` est la valeur de part : un apport ou un retrait externe crée ou détruit des parts au
  dernier prix de part connu, donc un dépôt n'améliore ni la performance ni le high-water mark
  (T45). `external_cashflow_cum` isole ces flux.
* `strategy_pnl = variation d'équité − flux externes nets`.

#### `position_snapshots`

`account_scope`, `inst_id`, `as_of`, `source`, `signed_base_qty` (unités de base, signé),
`signed_contracts` (contrats, signé), `average_entry_price` (**dérivé** du coût ouvert),
`mark_price`, `liquidation_price` (`NULL` = **inconnu**, jamais « distance infinie »), `margin`,
`leverage`, `protection` (état de protection observé), `version` (optimiste).

`unrealized_pnl = signed_base_quantity × (mark_price − average_entry_price)`, équivalent à
`signed_base_qty × mark − open_cost`. Ouvrir un notionnel sur un dérivé n'est pas un achat au
comptant débitant la totalité de ce notionnel.

### 3.7 Risque, exploitation, bus interne

| Table | Champs | Sens |
|---|---|---|
| `risk_events` | `event_id`, `created_at`, `severity`, `reason_code`, `affected_scope`, `evidence`, `requested_action`, `observed_result` | `requested_action` et `observed_result` sont **deux champs** : une action demandée n'est pas une action effectuée |
| `risk_state` | `account_scope` (PK), `halt_level`, `halt_reason`, `halt_since`, `utc_day`, `day_start_equity`, `day_realized_loss`, `high_water_mark_unit`, `high_water_mark_equity`, `limits_version`, `updated_at`, `version` | État de protection **persistant** : un redémarrage recharge halt, perte journalière et high-water mark et ne remet rien à zéro (T44). La frontière journalière est UTC et déclarée |
| `runtime_leases` | `lease_name` (PK), `holder`, `fencing_token`, `acquired_at`, `heartbeat_at`, `expires_at` | Bail du writer unique. `fencing_token` n'augmente qu'à chaque **nouvelle** acquisition : un ancien titulaire garde un jeton périmé que la base refuse (T46/T47) |
| `outbox_events` | `id`, `aggregate_type`, `aggregate_id`, `event_type`, `payload`, `idempotency_key` (unique), `created_at`, `status`, `claimed_by`, `claimed_until`, `fencing_token`, `attempts`, `processed_at`, `last_error` | Écrit dans la **même transaction** que l'écriture métier. Livraison « au moins une fois » : aucun « exactly once » n'est annoncé |
| `consumer_offsets` | `consumer_name` (PK), `last_event_id`, `updated_at` | Avance **vers l'avant uniquement** : une relivraison est ignorée |
| `operator_actions` | `request_id` (PK), `action`, `scope`, `reason`, `actor`, `role`, `requested_at`, `status`, `observed_result`, `completed_at` | Toute action opérateur **et tout refus** (statut `DENIED`) y sont tracés, y compris les refus d'authentification et de CSRF |

---

## 4. Contrats de domaine (`src/okxq/domain/events.py`)

Tous immuables (`frozen=True`), champs inconnus refusés (`extra="forbid"`), horodatages
timezone-aware (`AwareDatetime`), identifiants non vides. `canonical_hash()` produit l'empreinte
canonique d'un contrat.

| Contrat | Champs essentiels | Ce qu'il garantit |
|---|---|---|
| `EventEnvelope` | `event_id`, `event_type`, `schema_version` (≥ 1), `source`, `exchange_ts` (nullable), `receive_ts`, `available_at`, `ingest_seq` (≥ 0), `payload_hash`, `payload` | `available_at >= receive_ts` : une donnée n'est pas utilisable avant d'être reçue |
| `MarketSnapshot` | `snapshot_id`, `cutoff_at`, `universe_version`, `metadata_version`, `equity_version`, `eligible_instruments`, `held_instruments`, `book_versions`, `feature_versions`, `quality_flags`, `features`, `reference_prices` | Une vue **cohérente** : versions d'univers, de métadonnées, de carnet et d'équité figées ensemble |
| `FeatureVector` | `instrument`, `cutoff_at`, `available_at`, `names`, `values`, `masks`, `schema_hash` | Listes alignées, noms uniques, et surtout : masque `OK` ⇒ valeur présente, masque non-`OK` ⇒ valeur `None`. Aucune imputation silencieuse |
| `Forecast` | `forecast_id`, `model_id`, `snapshot_id`, `instrument`, `horizon_s` (> 0), `gross_mu`, `quantiles`, `uncertainty` (≥ 0), `execution_policy_id`, `cost_basis`, `available_at`, `fold_id`, `producer` | **Aucun champ ne donne de permission d'exchange.** Le rendement est brut ; les coûts sont ailleurs |
| `EdgeEstimate` | `edge_id`, `forecast_id`, `instrument`, `side`, `horizon_s`, `cost_basis`, `expected_gross_return`, `components`, `included_in_price`, `net_edge`, `uncertainty`, `uncertainty_penalty`, `edge_score`, `expires_at` | `cost_basis` + `included_in_price` rendent le double comptage détectable : ce que le prix contient déjà n'est pas déduit une seconde fois (T24) |
| `PortfolioTarget` | `target_id`, `snapshot_id`, `equity_version`, `signed_weights`, `constraints_version`, `solver_status`, `objective_value`, `active_constraints`, `residuals`, `solve_ms`, `created_at`, `expires_at`, `reason_codes` | Cible **datée et périssable** : `expires_at` empêche d'agir sur une cible obsolète |
| `OrderIntent` | `intent_id`, `decision_id`, `target_id`, `account_scope`, `inst_id`, `side`, `contracts` (> 0), `price_limit` (> 0 ou `None`), `order_type`, `reduce_only`, `ttl_ms` (> 0), `reason`, `client_order_id`, `created_at`, `expires_at` | Un ordre à prix limité **exige** `price_limit` ; un market en **interdit** un. `normalized_payload()` est le payload exact qui sera envoyé, et c'est lui qui est haché |
| `RiskDecision` | `decision_id`, `intent_id`, `intent_hash`, `action`, `allowed_payload_hash`, `allowed_contracts`, `limits_version`, `position_version`, `reservations`, `created_at`, `expires_at`, `reason_codes` | `ALLOW`/`REDUCE` portent obligatoirement un hash autorisé ; `REJECT`/`FLATTEN` n'en portent aucun — un refus ne peut pas se transformer en autorisation par omission |
| `ApprovedOrder` | `intent`, `decision`, `payload_hash` | Se construit **uniquement** si la décision porte sur cette intention, si l'action est `ALLOW`/`REDUCE`, et si les trois hashes coïncident. C'est le seul objet qu'un gateway accepte |
| `OrderEvent` | `event_id`, `client_order_id`, `exchange_order_id`, `event_kind`, `observed_state`, `cumulative_filled` (≥ 0), `average_fill_price`, `event_ts`, `receive_ts`, `raw_hash`, `reason` | Sépare l'horodatage exchange de la réception locale |
| `Fill` | `execution_key`, `account_scope`, `inst_id`, `client_order_id`, `exchange_order_id`, `trade_id`, `side`, `contracts` (> 0), `fill_price` (> 0), `fee_cashflow` (signé), `fee_ccy`, `fill_at`, `receive_ts`, `liquidity` | Unité d'autorité de la comptabilité |
| `RiskEvent` | `event_id`, `severity`, `reason_code`, `affected_scope`, `evidence`, `requested_action`, `observed_result`, `created_at` | `evidence` porte la preuve, pas un message libre |
| `SubmissionResult` | `intent_id`, `client_order_id`, `outcome` (`ACK` / `REJECTED` / `UNKNOWN` / `NOT_SENT`), `exchange_order_id`, `sent_at`, `ack_at`, `error_code`, `message` | `UNKNOWN` est un résultat de premier ordre, pas une erreur à réessayer |
| `ReconciliationReport` | `started_at`, `completed_at`, `orders_checked`, `unknown_resolved`, `unknown_remaining`, `balance_gap`, `position_mismatches`, `ok`, `notes` | Un écart est **mesuré** (`balance_gap`), pas écrasé par le dernier solde |
| `SourceDocument` | `document_id`, `source`, `source_url`, `published_at` (nullable), `first_seen_at`, `received_at`, `parsed_at`, `language`, `version` (≥ 1), `date_method`, `raw_text_hash`, `deduplication_id`, `asset_mapping`, `mapping_quality`, `title`, `text` | `received_at >= first_seen_at`, `parsed_at >= received_at`. `mapping_quality` (`ok`, `ambiguous`, `ticker_only`, `none`, ou `None` si non tenté) rend l'incertitude de correspondance explicite (T54) |
| `AssetMapping` | `inst_id`, `canonical_name`, `symbol`, `mapping_version`, `confidence` (∈ [0,1]), `method` | Un ticker seul n'est pas une identité : `method` dit comment la correspondance a été obtenue |
| `JevAnswer` | `type` (`choice` / `noul` / `score`), `choice`, `probabilities`, `probability`, `score`, `score_distribution`, `confidence` | Toutes les probabilités sont bornées dans [0,1] et finies. `confidence` est une propriété de la **réponse sémantique**, pas une probabilité de profit |
| `JevEvaluation` | `evaluation_id`, `document_id`, `document_version`, `question_set_hash`, `asset_mapping_version`, `model_requested`, `model_effective`, `requested_at`, `completed_at`, `inference_completed_at`, `features_committed_at`, `answers`, `usage`, `status` (`ok` / `late` / `error` / `rejected`), `error`, `inst_id` | Jamais antidatée ; un statut `ok` exige `features_committed_at`. `late` est un état explicite : la réponse est archivée et **exclue** du snapshot courant |
| `PortfolioInputs` | `instruments`, `mu`, `sigma`, `w0`, `cost_buy`, `cost_sell`, `uncertainty_penalty`, `expected_funding_cost`, `future_exit_cost`, `asset_limit`, `liquidity_capacity`, `beta_btc`, `beta_eth`, `clusters`, `horizon_s`, bornes de risque, `margin_capacity`, `margin_requirement_per_unit`, `equity_version`, `snapshot_id`, `constraints_version` | Toutes les listes sont validées alignées sur `instruments`, `sigma` carrée, et un cluster ne peut pas référencer un instrument absent |

Énumérations : `QualityFlag` (`OK`/`MISSING`/`STALE`/`INVALID`), `CostBasis` (`MID`/`EXECUTABLE`),
`PositionSide` (avec `sign`), `RiskAction`, `Severity`, `OrderEventKind`, `Liquidity`, `JevStatus`,
`JevAnswerType`, `SubmissionOutcome`, et `Side` (`buy`/`sell`, avec `sign` et `opposite`) dans
`okxq.domain.money`.

---

## 5. Unités : les confusions que ce schéma interdit

| Grandeur | Unité | Piège évité |
|---|---|---|
| `contracts` | contrats | Les volumes en contrats ne sont **pas** comparables entre instruments |
| `signed_base_qty` | unités de base (BTC, ETH…) | `signed_base_quantity = signed_contracts × v` |
| `notional_usdt` | USDT | `signed_contracts × v × prix de référence` |
| `signed_weights` | fraction de l'équité | `raw_target_contracts = poids × equity / (v × prix)` |
| `fee_cashflow` | devise `fee_ccy` | Signé ; `fee_cost = -fee_cashflow` pour la décision |
| Rendement de label | fraction du **notionnel d'entrée** | `gross_return_on_entry_notional = s × (P1/P0 − 1)` — ce n'est ni le rendement du capital du compte ni celui de la marge |
| `horizon_s`, `ttl_ms`, `*_seconds`, `*_ms` | l'unité est dans le nom | Aucune durée sans unité nommée |
| Bandes de profondeur | points de base | Une bande absolue n'est pas comparable entre actifs |

---

## 6. Ce que ce dictionnaire ne couvre pas

Par honnêteté sur l'état du dépôt :

* **Aucune donnée de marché réelle.** Les tables n'ont été peuplées que par des tests et par les jeux
  synthétiques (`src/okxq/research/synthetic.py`) ou golden (`tests/fixtures/golden*/`). Les
  `available_at` des jeux synthétiques sont **supposés**, pas mesurés : ils portent le drapeau
  `LATENCY_ASSUMED`.
* **Aucun chiffre de ces tables n'est une preuve d'avantage de marché.** Un PnL calculé sur données
  synthétiques mesure le pipeline, pas le marché.
* **Migrations non exécutées contre PostgreSQL dans cette session.** La migration `0001_initial`
  existe et `tests/unit/test_migrations.py` vérifie hors ligne qu'elle produit le même schéma que
  `Base.metadata` (tables, colonnes, nullabilité, unicité, contraintes, clés étrangères, index) et
  qu'elle est réversible — mais **sur SQLite**. SQLite ne distingue ni `TIMESTAMPTZ` de `DATETIME`,
  ni `JSONB` de `JSON`, et n'exerce ni `FOR UPDATE SKIP LOCKED` ni la précision `NUMERIC` réelle. La
  vérification des types réellement émis sur PostgreSQL est marquée `integration` et reste `NOT_RUN`
  sans `OKXQ_TEST_DATABASE_URL`.
* **Aucune évaluation JEV réelle** : `jev_requests` / `jev_results` n'ont jamais reçu de réponse du
  fournisseur (aucune clé TypeSafe dans cette session).
* Les définitions de **features** (nom, version, source, sémantique temporelle, fenêtre, unité,
  politique d'absence) vivent dans `src/okxq/features/definitions.py` et `registry.py` et sont
  persistées par empreinte dans `feature_schemas` ; le catalogue nominatif complet des features n'est
  pas reproduit ici.
