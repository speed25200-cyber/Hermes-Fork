# ADR-001 — Choix structurants de la plateforme

**Statut** : accepté (18 septembre 2026).

**Portée** : les sept décisions dont dépend la forme de tout le reste du code. Chacune est écrite au
format contexte / décision / conséquences / alternatives écartées. Les ADR suivants raffinent des
sous-domaines : ADR-002 (persistance), ADR-003 (déploiement et secrets par service), ADR-004 (bus
interne). `DECISIONS.md` référence ce document pour les décisions D1, D5 et D7.

Ces décisions sont des choix de **conception**. Aucune n'est une affirmation de performance, et
aucune ne vaut preuve d'un avantage de marché.

---

## D-1 — `Decimal` partout pour l'argent, `float` interdit à l'entrée

### Contexte

Un montant monétaire en binaire flottant n'est pas représentable exactement : `0.1 + 0.2 != 0.3`. Sur
une plateforme de trading, l'erreur ne reste pas dans l'affichage — elle entre dans une position, un
prix moyen d'entrée, un coût de base, puis dans une décision de risque. Elle est ensuite
indiscernable d'une véritable divergence de réconciliation, ce qui est le pire résultat : on ne sait
plus si l'écart vient du marché ou de l'arithmétique. Le cahier des charges impose des décimaux
sérialisés en chaînes (§43) et des `NUMERIC` de précision explicite (§44).

### Décision

* Tous les montants, prix et quantités sont des `Decimal`. `okxq.domain.money.dec()` **refuse** un
  `float`, un booléen, un NaN et un infini. La seule conversion depuis un flottant est
  `dec_from_float(x, places)`, qui **déclare** sa quantification — un appel explicite, pas une
  coercition silencieuse.
* Les unités sont portées par des types distincts : `Money` (avec sa devise ; deux devises ne
  s'additionnent pas), `Contracts` (nombre signé de contrats), `BaseQty` (quantité signée en unité de
  base). Un notionnel, une quantité de base et un nombre de contrats ne sont jamais le même objet.
* Les arrondis ont une **direction nommée** : `round_down_to_step` / `round_up_to_step`,
  `round_price_passive` (un achat vers le bas, une vente vers le haut — on ne franchit pas le carnet
  par accident) et `round_price_aggressive_within_limit` (arrondi vers l'exécution, mais jamais
  au-delà de la limite préapprouvée). **Jamais d'arrondi « au plus proche » sur une taille de
  risque** : une augmentation d'exposition s'arrondit vers le bas.
* En base : `NUMERIC(28,8)` pour les montants, `NUMERIC(28,12)` pour prix et quantités, avec un test
  de dépassement (`check_numeric_bounds`).
* En sortie d'API et de journaux, les décimaux sont des **chaînes** ; les flottants restent réservés
  aux sorties statistiques, validées finies (`FiniteFloat`).
* Corollaire comptable : la position stocke `open_cost` (somme signée exacte des notionnels d'entrée)
  et **dérive** `average_entry_price = |open_cost / qty|`. Le prix moyen est un affichage ; le coût
  ouvert fait autorité. À la fermeture totale, le coût libéré est **exactement** `open_cost`, donc
  l'erreur de prorata (≤ 1e-12) est réalisée exactement et ne s'accumule pas.

### Conséquences

* Le code est plus verbeux : chaque conversion est explicite, et les sorties de modèles doivent
  passer par `dec_from_float` avec un nombre de décimales déclaré.
* Les modules de recherche (`numpy`, `polars`, LightGBM) travaillent en flottants — c'est assumé et
  la frontière est nette : la recherche produit des **fractions** de notionnel, jamais des montants.
  La conversion en argent appartient au ledger.
* L'identité « flux de trésorerie + valeur marquée = PnL réalisé + PnL latent » est vérifiable, et
  elle est vérifiée par le test de propriété `test_position_conservation` et par les invariants du
  parcours hors ligne.
* Les signes sont préservés : `fee_cashflow` est signé et aucun `abs()` n'est appliqué, sans quoi un
  rebate maker deviendrait un coût.

### Alternatives écartées

| Alternative | Pourquoi écartée |
|---|---|
| `float` avec arrondi à l'affichage | L'erreur entre quand même dans la comptabilité et le risque ; elle devient indiscernable d'un écart réel |
| Entiers en unités minimales (satoshis, ticks) | Correct, mais les tailles de contrat et les pas de prix varient par instrument et **changent** en cours de vie : il faudrait reconvertir à chaque changement de métadonnée, et une conversion ratée serait silencieuse |
| `Decimal` seulement à la frontière, `float` à l'intérieur | C'est exactement l'endroit où l'erreur se fabrique ; la frontière ne tient que si personne ne l'oublie jamais |

---

## D-2 — Contrats Pydantic immuables, champs inconnus refusés

### Contexte

§43 demande des objets immuables typés, des identifiants non vides et des horodatages
timezone-aware. Le risque n'est pas théorique : un dictionnaire muté entre l'approbation du risque et
l'envoi de l'ordre est précisément le scénario T49, et un champ ajouté par un fournisseur qu'on
ignore silencieusement est le scénario T53.

### Décision

Une classe de base unique, `okxq.domain.events.Contract` :

```python
model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)
```

* **`frozen=True`** : un contrat ne se modifie pas. Pour changer quelque chose, on construit un
  nouvel objet — donc un nouveau hash, donc une nouvelle validation là où un hash est exigé.
* **`extra="forbid"`** : un champ inconnu est une **erreur**, pas un champ ignoré. C'est ce qui fait
  qu'un changement de contrat côté fournisseur se voit immédiatement au lieu de se propager en
  silence.
* Les **validateurs portent les règles métier**, pas seulement les types :
  `EventEnvelope` exige `available_at >= receive_ts` ; `FeatureVector` exige des listes alignées, des
  noms uniques, et surtout masque `OK` ⇒ valeur présente / masque non-`OK` ⇒ valeur `None` (aucune
  imputation silencieuse) ; `OrderIntent` exige `price_limit` pour un ordre limité et l'**interdit**
  pour un market ; `RiskDecision` exige un hash autorisé sur `ALLOW`/`REDUCE` et l'**interdit** sur
  `REJECT`/`FLATTEN` ; `JevEvaluation` refuse toute antidatation ; `PortfolioInputs` vérifie
  l'alignement de toutes ses listes et la forme carrée de la covariance.
* `ApprovedOrder` est un contrat qui ne peut **exister** que si la décision porte sur cette
  intention, si l'action l'autorise, et si les trois hashes coïncident.
* `canonical_hash()` / `payload_hash()` produisent une empreinte canonique stable, qui est ce qui est
  approuvé.
* La configuration suit la même discipline (`StrictModel` : `extra="forbid"`, `frozen=True`), et
  certaines exigences sont typées `Literal` pour qu'elles ne soient **pas** désactivables :
  `require_frozen_final_test: Literal[True]`, `promotion_auto_approve: Literal[False]`,
  `auto_resume_after_critical_halt: Literal[False]`, `timezone_storage: Literal["UTC"]`.

### Conséquences

* Une erreur de contrat survient **à la construction**, avec un message qui nomme le champ, plutôt
  qu'au dixième appel sous forme d'une valeur absurde.
* Une tolérance à un champ inconnu ajouté par un fournisseur demande une modification de code
  explicite — c'est voulu, et cela ne dispense jamais de traiter l'absence d'un champ obligatoire.
* Le coût de validation Pydantic est payé à chaque frontière. Il est négligeable devant le budget
  d'une décision d'une minute, et il est mesuré (`timings_ms` de `decisions`).
* Les objets étant gelés, tout état mutable est explicite : projections en base avec version
  optimiste, et `dataclass(frozen=True)` + `replace()` dans le domaine.

### Alternatives écartées

| Alternative | Pourquoi écartée |
|---|---|
| `dict` + JSON Schema | La validation devient facultative en pratique, et rien n'empêche une mutation entre deux étapes |
| `dataclass` simple sans validation | Pas de validation des règles métier, pas de sérialisation canonique, `extra` ingérable |
| `attrs` / protobuf | Équivalents mais redondants avec Pydantic déjà requis pour la configuration (§41) ; ne pas multiplier les bibliothèques équivalentes |
| `extra="ignore"` | Un champ obligatoire disparu et un champ inconnu apparu se ressemblent alors : on perd la seule occasion de détecter un changement de contrat |

---

## D-3 — Un seul écrivain d'exécution, avec bail et jeton de cloisonnement

### Contexte

Deux processus qui signent et envoient des ordres simultanément produisent des doublons, une
exposition double et une comptabilité fausse — et le redémarrage est précisément le moment où cela
arrive (l'ancienne instance n'est pas encore morte, la nouvelle démarre). Un verrou en mémoire ne
protège rien entre deux processus ; un verrou consultatif sans jeton ne protège pas de l'ancien
titulaire qui revient après une pause (T46, T47).

### Décision

* Un **unique** gateway d'exécution (`src/okxq/execution/gateway.py`) : le seul chemin de code qui
  envoie un ordre à un exchange. Le gateway refuse un adaptateur réel hors DEMO/LIVE.
* Un **bail durable en base** (`runtime_leases`) avec heartbeat et un **jeton de cloisonnement**
  (`fencing_token`) monotone, qui n'augmente qu'à chaque **nouvelle** acquisition
  (`src/okxq/execution/leadership.py`). Un ancien titulaire conserve un jeton périmé que la base
  refuse.
* `assert_leader` **relit la base dans la transaction d'envoi**. Ce point est le cœur de la
  décision : vérifier le leadership avant d'envoyer ne suffirait pas, car le bail peut expirer entre
  la vérification et l'envoi. Une instance `FOLLOWER` n'a aucun chemin d'envoi.
* Une perte de base (exception au heartbeat) fait passer localement en `LOST` : aucun envoi avant
  réacquisition. **Une instance qui ne sait pas si elle est leader se comporte comme si elle ne
  l'était pas.**
* La réclamation d'événements d'outbox est fencée par le même jeton, et la clôture d'un événement est
  conditionnelle au bail.

### Conséquences

* Le débit d'envoi est celui d'un seul processus. C'est acceptable : la cadence de décision est de
  60 s, et la sécurité prime sur le parallélisme à cet endroit.
* Une bascule d'instance demande d'attendre l'expiration du bail. C'est un délai assumé — préférable
  à deux écrivains.
* Cette décision **exige** PostgreSQL (verrous et `FOR UPDATE SKIP LOCKED`), donc les tests de
  concurrence réels sont marqués `integration` et restent `NOT_RUN` sans base. T46 et T47 n'ont
  aujourd'hui aucun test nommé : le mécanisme est écrit, le scénario n'est pas exercé
  (`docs/test_matrix.md`).
* Aucune garantie « exactly-once » n'est annoncée : l'envoi est « au plus une fois » par tentative, et
  la vérité est celle de l'exchange, obtenue par les événements privés et la réconciliation.

### Alternatives écartées

| Alternative | Pourquoi écartée |
|---|---|
| Verrou en mémoire ou fichier de PID | Ne protège pas entre machines, ni d'un processus zombie qui reprend la main |
| Élection via Redis/etcd/Consul | Une dépendance d'infrastructure supplémentaire pour un problème que la base — déjà présente et déjà source de vérité financière — résout |
| Verrou consultatif PostgreSQL sans jeton | Un ancien titulaire qui revient après une pause GC peut encore envoyer : c'est exactement T47 |
| Plusieurs écrivains partitionnés par instrument | Déplace le problème sur la marge et les budgets de risque, qui sont **globaux** au compte |

---

## D-4 — Boîte d'envoi transactionnelle (outbox), pas de courtier de messages

### Contexte

Écrire un ordre en base **puis** publier un message est un double écrit : un crash entre les deux
perd le message ou crée un ordre fantôme. §41 autorise un bus mémoire borné intra-processus, mais
exige un journal durable et une outbox transactionnelle pour les intentions, les ordres et les fills.
Kafka et Kubernetes ne sont pas obligatoires pour une première version.

### Décision

* L'événement est écrit dans la **même transaction** que l'écriture métier, via
  `UnitOfWork.outbox` (`src/okxq/execution/outbox.py`). Une seule transaction contient : intention +
  approbation + ordre (`RISK_APPROVED`) + réservation pessimiste + événement `order.submit` portant
  l'`ApprovedOrder` exact.
* Un worker réclame l'événement avec un bail durable (`claimed_by`, `claimed_until`,
  `fencing_token`) ; un bail expiré est relivré ; la clôture est conditionnelle au bail.
* L'idempotence repose sur `idempotency_key` (unique) et sur des handlers qui **doivent** tolérer une
  relivraison : la livraison est « **au moins une fois** », jamais « exactement une fois » à travers
  le réseau. `consumer_offsets` n'avance que **vers l'avant** : une relivraison est ignorée.
* Séquence d'envoi : la tentative et le payload **exact** sont marqués en base et **commités**, puis
  un seul `place_order`. Une réponse perdue donne `UNKNOWN` avec la réservation **maintenue**, aucun
  retry aveugle, et réconciliation exigée avant toute nouvelle entrée (T32, T33).
* Les flux de marché **publics** passent par des files asyncio bornées intra-processus, avec des
  compteurs de pertes : une perte se voit, elle n'est pas masquée par un compteur absent.

### Conséquences

* Aucun courtier à exploiter, sauvegarder ni sécuriser : la base est déjà la source de vérité
  financière, donc la sauvegarder suffit.
* La latence d'envoi inclut un commit. C'est le prix de la durabilité, et il est assumé à cette
  cadence.
* Les handlers doivent être écrits idempotents — contrainte réelle, mais c'est de toute façon
  nécessaire face à un exchange qui peut redélivrer.
* `UNKNOWN` devient un état de première classe, visible dans l'interface et pris en compte de façon
  **pessimiste** par le Risk Engine (T43).

### Alternatives écartées

| Alternative | Pourquoi écartée |
|---|---|
| Kafka / NATS / RabbitMQ | Hors périmètre de la première version (§41) ; ajoute un système à exploiter et **ne supprime pas** le double écrit sans outbox de toute façon |
| Publier après commit, sans outbox | Un crash entre le commit et la publication perd l'événement |
| File en mémoire pour les ordres | Un redémarrage perd des intentions ; inacceptable pour des ordres, acceptable pour des prix publics (d'où la distinction ci-dessus) |
| Promettre « exactly once » | Impossible à travers un réseau ; l'annoncer créerait une fausse confiance et découragerait la réconciliation |

---

## D-5 — JEV isolé, et jamais dans le chemin critique

### Contexte

JEV / TypeSafe AI est un composant sémantique **spécialisé**, pas une source d'alpha par défaut : son
apport doit être démontré expérimentalement (§12, §40). Deux risques distincts : mettre un appel
réseau tiers dans une boucle qui doit conclure en moins d'une minute, et laisser un document non
fiable influencer la protection ou l'exécution.

### Décision

* **Un processus séparé** (`src/okxq/jev/worker.py`, rôle `jev-worker`) : file bornée, concurrence
  limitée, deadline **murale** totale, disjoncteur, budget financier journalier. Un timeout de socket
  n'est pas une deadline murale — les deux existent et sont distincts
  (`src/okxq/jev/client.py`).
* **Aucune référence** du worker vers le risque, l'exécution, les positions ou l'exchange. En panne,
  il rend une évaluation `error`/`rejected` avec sa raison et **ne lève jamais** vers l'appelant.
* **Hors du chemin critique** : `src/okxq/runtime/decision_loop.py` ne contient **aucune** référence
  à JEV. Les features sémantiques sont lues comme n'importe quelle autre feature, à partir
  d'évaluations dont `features_committed_at <= cutoff` et dont le statut est `ok`
  (`src/okxq/features/events.py`). Une réponse arrivée trop tard est archivée `late` et **exclue** du
  snapshot courant.
* **Séparation des secrets** : le worker ne reçoit que sa propre clé. Le rôle `jev-worker` refuse de
  démarrer si une clé d'échange est présente dans son environnement
  (`assert_credentials_separation`). La requête sortante est un contrat strict : aucune position,
  aucun patrimoine, aucune identité du titulaire, aucune stratégie privée, aucun identifiant
  d'exchange.
* **Le document est une donnée, jamais une instruction.** Les gabarits de questions le disent
  explicitement, le mapping actif ↔ document passe par un registre point-in-time (JEV juge la
  pertinence d'un candidat **déjà identifié**, il ne choisit pas librement un instrument
  exécutable), et les comparaisons de dates, conversions d'unités et calculs de montants restent
  déterministes hors du modèle.
* **Une analyse JEV ne déplace jamais un stop et ne crée jamais un ordre.** Structurellement, un
  ordre n'existe que par `OrderIntent` → `RiskDecision` → `ApprovedOrder`. Et une panne JEV n'est
  **pas** un déclencheur de protection : `src/okxq/risk/kill_switch.py` accepte le signal
  `jev_unavailable` et l'ignore explicitement — il relève de la politique d'entrées, pas du halt.
* **Une absence n'est pas un zéro** : `jev_available`, `jev_age`, la raison d'absence et la qualité
  sont explicites. Le modèle de secours sans JEV doit être entraîné et validé **comme tel** ; on
  n'utilise pas un modèle qui exige JEV en remplaçant arbitrairement ses entrées.
* Le cache est indexé par `(model_version, question_hash, document_version, asset_mapping_version,
  cleaning_pipeline_version)` — et **un cache ne réinitialise pas l'âge d'un événement**.

### Conséquences

* Une panne, une lenteur ou un changement de version du fournisseur ne peut pas retarder une
  décision ni bloquer la protection : au pire, les features sémantiques sont `MISSING` avec leur
  raison.
* Les features JEV sont structurellement **en retard** sur l'événement (réception, inférence,
  publication). C'est correct et c'est voulu : une feature datée à l'heure déclarée de publication
  serait une fuite.
* Si JEV n'améliore pas l'utilité nette ou la protection, il reste en **shadow** ou son influence est
  désactivée, l'intégration et les rapports étant conservés. Aucun poids non nul n'est forcé pour
  pouvoir annoncer « IA avec JEV ».
* **Aucun appel JEV réel n'a eu lieu dans cette session** (aucune clé TypeSafe) : la conformité du
  fournisseur, sa version effective, sa facturation et sa dégradation sont NOT_RUN, comme T50 et T51.

### Alternatives écartées

| Alternative | Pourquoi écartée |
|---|---|
| Appel synchrone dans la boucle de décision | Un fournisseur lent devient une décision manquée ; le budget d'une minute ne peut pas dépendre d'un tiers |
| JEV dans le même processus que la stratégie | Il verrait son environnement, donc ses secrets ; et une dépendance compromise du client HTTP toucherait la stratégie |
| Fine-tuning local d'un modèle sémantique | Hors périmètre et hors contrat du service (§49) |
| Laisser JEV proposer l'instrument à trader | Une chaîne de caractères ne doit jamais se transformer en instrument exécutable ; c'est le registre point-in-time qui identifie, pas le modèle |
| Utiliser JEV comme juge de sa propre qualité | Ne mesure rien ; l'évaluation passe par un corpus annoté séparé |

---

## D-6 — LIVE derrière un manifeste signé

### Contexte

§1 et §71 imposent que LIVE soit désactivé par défaut et exige trois gates franchis séparément
(technique, scientifique, opérateur/compte), vérifiés par un mécanisme **authentifié/signé** — pas
par un fichier YAML librement éditable. Le risque réel n'est pas un attaquant : c'est un agent ou un
opérateur qui active LIVE « pour terminer une démonstration ».

### Décision

* `project.live_enabled` vaut `false` par défaut ; le profil livré est `configs/live.disabled.yaml`.
* Un **manifeste JSON signé HMAC-SHA256** par `OPERATOR_AUTH_SECRET` (secret serveur, jamais dans
  Git) lie : compte, environnement, commit du code, hash de configuration, hashes d'artefacts,
  limites propres au capital, date d'émission, date d'**expiration**, acteur, et les trois gates avec
  leurs preuves (`src/okxq/config/live_guard.py`, signé par
  `scripts/sign_approval_manifest.py`).
* `verify_live_authorization()` vérifie, dans l'ordre : profil activé, secret présent, fichier
  présent, champs obligatoires **complets**, **signature valide**, non-expiration, environnement
  `LIVE`, compte identique, `config_hash` identique à la configuration chargée, `code_commit`
  identique au code déployé, les **trois** gates marqués franchis, et des limites au moins aussi
  strictes que la configuration. Toute modification de l'un de ces éléments invalide la signature.
* `LiveAuthorization` est la **preuve** vérifiée : son `__post_init__` refuse de se construire sans
  preuve de signature, et `authorize_live()` est son seul constructeur. L'adaptateur OKX **exige** cet
  objet avant toute connexion privée (T64).
* DEMO et LIVE sont deux instances distinctes, le drapeau étant fixé à la **construction** : un échec
  DEMO ne bascule jamais vers LIVE (T63). Le profil de région est exigé ; profil absent = refus.
  Jamais d'URL « universelle » choisie pour contourner une restriction.
* Il n'existe **aucune** route d'API d'activation LIVE, **aucune** option `--force`, et l'adaptateur
  ne modifie jamais un mode de compte ni un levier — ces endpoints sont interdits par construction
  dans le client REST.
* `fixture_only` est interdit hors PAPER/RESEARCH : une configuration de smoke ne peut pas démarrer
  DEMO ou LIVE.

### Conséquences

* Activer LIVE demande une action humaine, hors du dépôt, avec un secret que le modèle n'a pas. C'est
  l'objectif : le rôle de l'agent constructeur s'arrête à préparer et tester cette mécanique.
* Chaque changement de code, de configuration, de limites, de compte ou de modèle **invalide**
  l'approbation. C'est une friction voulue : une approbation de version ne vaut pas pour toute mise à
  jour ultérieure.
* Le manifeste a une expiration : une approbation oubliée cesse d'être valable.
* La mécanique est implémentée et testée hors ligne (T64 : deux cas verts), mais **jamais exercée en
  réel** : le preflight connecté et le démarrage à exposition bornée sont NOT_RUN. **Aucun gate n'est
  franchi et aucun manifeste n'existe** (`docs/live_readiness.md`).

### Alternatives écartées

| Alternative | Pourquoi écartée |
|---|---|
| `live_enabled: true` dans un YAML | §71.3 l'exclut explicitement : un fichier librement éditable par le modèle n'est pas une approbation |
| Variable d'environnement `OKXQ_LIVE=1` | Trop facile à poser par accident ou par un script de déploiement ; ne lie ni le commit, ni la configuration, ni les limites |
| Route d'API protégée par rôle admin | Une session de navigateur compromise activerait LIVE ; et cela ne lie rien aux gates |
| Signature asymétrique (clé opérateur privée) | Plus forte, mais exige une gestion de clés et un stockage matériel non disponibles ici ; HMAC avec un secret serveur est le maximum réellement applicable dans ce périmètre. **À reconsidérer** avant une mise en service réelle |

---

## D-7 — L'interface graphique d'Hermes est conservée et étendue

### Contexte

Le propriétaire a demandé explicitement de garder la même interface graphique. §41 proposait
React + TypeScript + Vite ; §59 exige que l'affichage vienne des vrais endpoints du backend, que le
mode soit visible en permanence, qu'une métrique inconnue s'écrive « Non disponible » et non zéro, et
qu'un écran de données synthétiques porte une mention persistante. L'interface Hermes existante
(HTML/JS vanille, trilingue FR/EN/SQ, thème sombre/clair) satisfait déjà ces exigences de forme.

### Décision

* `frontend/` reprend **tels quels** `index.html`, `vue.js`, `graphe.js`, `langues.js`, `pont.js` et
  les polices. Aucun React, aucun Vite, aucune étape de construction.
* Un seul fichier est ajouté, `pages.js`, qui apporte les quatre vues de la plateforme — Décisions,
  Recherche, JEV, Risque — dans **exactement** le même langage visuel : mêmes blocs, mêmes tuiles,
  mêmes jetons de couleur, mêmes animations d'entrée. Elles remplacent l'onglet Laboratoire.
* `pont.js` conserve la surface d'Hermes (`api.invoke`, `api.on`, flux SSE) et l'API expose une
  couche de compatibilité `POST /api/<canal>` + `/api/flux`
  (`src/okxq/api/routes/compat.py`), pour que la page reste **la même page**.
* Le contrat UI/API est **verrouillé des deux côtés** par `tests/contract/test_ui_api_contract.py`
  (l'API publie les clés lues **et** `pages.js` cite bien ces noms) et vérifié dans un **vrai
  navigateur** par `tests/e2e/test_ui_smoke.py`.

### Conséquences

* Zéro chaîne de construction frontend, zéro `node_modules` en production : la page est servie
  statiquement par l'API. Les tests frontend tournent avec `node --test` et un smoke Playwright.
* Les tests frontend `node --test` ne sont pas dans la suite pytest, donc ils n'apparaissent pas dans
  `reports/junit.xml` ni dans `docs/test_matrix.md` (`make ui-test`).
* Le vocabulaire visuel hérité impose des contraintes : le journal du moteur reste non traduit
  (c'est la voix technique du serveur ; le traduire fabriquerait deux vérités), et du balisage
  Hermes subsiste sans être atteignable (`page-labo`).
* Un renommage de clé côté API ne casse pas la page : elle affiche « Non disponible ». C'est
  précisément pourquoi le test de contrat existe — ce défaut silencieux s'est réellement produit
  (bloc publié sous `halts`, lu sous `risk`).
* **T65** (action UI non autorisée / CSRF) reste `NOT_RUN` : le mécanisme est écrit dans
  `src/okxq/api/auth.py` mais aucun test ne porte cet identifiant. Voir `docs/ui.md` et
  `docs/threat_model.md`.

### Alternatives écartées

| Alternative | Pourquoi écartée |
|---|---|
| Réécrire en React + TypeScript + Vite (§41) | Demande explicite du propriétaire de conserver l'interface ; et une réécriture aurait perdu le travail existant sans gain fonctionnel |
| Deux interfaces (Hermes + une nouvelle) | Deux vérités affichées sur le même compte : le pire résultat possible pour un tableau de bord d'exploitation |
| Étendre par des pages hors du langage visuel | Une interface hétérogène se lit plus lentement, et une page d'exploitation se lit sous stress |

---

## Références

* Cahier des charges : §1 (principes non négociables), §41 (stack et frontières), §43 (contrats),
  §44 (schéma), §45 (unités et arrondis), §50 (JEV), §52 (ordres durables, idempotence,
  concurrence), §59 (tableau de bord), §60 (sécurité), §71 (gates avant LIVE).
* `DECISIONS.md` (D1, D3–D9), `docs/adr/ADR-002`, `ADR-003`, `ADR-004`.
* `docs/data_dictionary.md`, `docs/threat_model.md`, `docs/ui.md`, `docs/strategy_research.md`,
  `docs/test_matrix.md`, `docs/live_readiness.md`.
