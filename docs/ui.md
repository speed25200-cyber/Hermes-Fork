# Interface graphique

L'interface graphique d'Hermes est **conservée à l'identique et étendue**. Ce n'est pas une
réécriture : `frontend/index.html`, `vue.js`, `graphe.js`, `langues.js` et `pont.js` sont les fichiers
d'Hermes, avec leurs polices, leur thème, leurs animations et leur langage visuel. Un seul fichier
est ajouté, `pages.js`, qui apporte les quatre vues de la plateforme quantitative dans **exactement**
le même langage visuel. Aucun React, aucun Vite, aucune étape de construction : la page est servie
telle quelle (décision D1 de `DECISIONS.md`, voir `docs/adr/ADR-001-choix-structurants.md`).

Ce document décrit ce que les fichiers font réellement. Rien ici n'est une mesure de performance, et
aucun chiffre affiché par cette interface ne prouve un avantage de marché : dans cette session,
l'interface n'a été alimentée que par des fixtures et des jeux synthétiques.

---

## 1. Fichiers et rôles

| Fichier | Lignes | Rôle |
|---|---|---|
| `frontend/index.html` | ~1500 | Structure et **tout** le CSS, en ligne. En-tête, bandeau de données synthétiques, page Marché, les quatre nouvelles pages, la fenêtre de graphique. Le HTML porte le français en dur : c'est ce qui s'affiche avant que les scripts se chargent |
| `frontend/pont.js` | 140 | Le pont vers la plateforme : `window.api.invoke/get/post/on`, le flux SSE, le jeton CSRF, la gestion des refus |
| `frontend/langues.js` | ~510 | Dictionnaire trilingue FR/EN/SQ (**324 clés**) et application des traductions |
| `frontend/vue.js` | ~960 | Rendu de la page Marché conservée : équité, tuiles, cartes de positions, historique, courbe d'équité, santé, journal, en-tête |
| `frontend/graphe.js` | ~640 | Fenêtre de chandelles d'une position, avec ses niveaux (entrée, take-profit, stop, liquidation, prix) posés sur l'échelle |
| `frontend/pages.js` | ~440 | **Ajout** : les quatre vues Décisions, Recherche, JEV, Risque, la navigation, le jeton de mode, le bandeau synthétique, les confirmations et le suivi des commandes |
| `frontend/*.woff2` | — | Inter, Space Grotesk, JetBrains Mono, préchargées, servies localement (aucun appel à un CDN) |
| `frontend/tests/interface.test.js` | — | 8 tests `node --test` sur la source de l'interface |

Chargement, dans cet ordre (fin de `index.html`) : `pont.js`, `langues.js`, `vue.js`, `graphe.js`,
`pages.js`. L'API sert la page et les fichiers statiques depuis `frontend/`
(`src/okxq/api/app.py`) ; `/` et `/index.html` répondent en `no-store`.

---

## 2. Pages

### Marché (conservée d'Hermes)

Rendue par `vue.js`. Équité en bandeau principal avec son delta et une étincelle ; tuiles de
synthèse ; **cartes de positions** réconciliées par symbole ; historique des positions ; courbe
d'équité dépliable avec ses statistiques ; santé des composants ; journal du moteur.

Deux règles, écrites dans l'en-tête du fichier, gouvernent ce rendu :

* **la couleur ne porte jamais seule une information.** C'est une mesure, pas un principe : entre le
  vert `#0ca30c` et le rouge `#d03b3b`, l'écart tombe à 4,1 en deutéranopie — deux tons
  indiscernables pour une partie des lecteurs, et ce sont exactement ceux qu'une interface de trading
  pose sur le take-profit et le stop. Ils sont conservés pour le lecteur habitué, mais chaque repère
  porte **aussi** son mot, sa forme et sa position ;
* **la mise à jour se fait en place, jamais en reconstruisant.** La page se rafraîchit toutes les
  quatre secondes ; en remplaçant le HTML, chaque carte serait détruite puis recréée et son animation
  d'entrée rejouerait en boucle. Les cartes sont donc réconciliées par symbole : celles qui restent
  sont modifiées, celles qui arrivent entrent, celles qui partent sortent. C'est ce qui permet de
  **voir un stop glisser** vers son nouveau prix au lieu de constater qu'il a changé.

Deux avis contextuels y apparaissent :

* `avis-cles` — « Aucune clé d'exchange sur ce service » : la plateforme reçoit les prix publics et
  calcule ses décisions mais n'envoie aucun ordre, **donc un compte vide n'est pas une perte**. L'avis
  rappelle que les clés vivent dans l'environnement du seul service d'exécution et ne transitent ni
  par la page, ni par l'API, ni par JEV, et qu'aucun formulaire ne peut les remplacer ici.
* `avis-lecture` — « Lecture seule » : la page montre tout et ne commande rien.

### Décisions

`GET /api/v1/decisions?limit=60`. Tableau des décisions à la minute : instant de coupe, issue
(`TRADE` / `NO_TRADE` / `SKIPPED` / `FAILED`, avec pastille de couleur **et** libellé), raisons,
modèle, version d'univers, durée (somme des `timings_ms`). Second bloc : les **candidats rejetés et
leurs raisons**, avec avantage brut, avantage net, coûts, incertitude et liens de provenance. Une
absence de trade est une décision, et elle s'affiche comme telle.

### Recherche

`GET /api/v1/experiments?limit=40` et `GET /api/v1/models?limit=40`. Un avis permanent en tête :
« Résultats de recherche, pas de promesses — une expérience n'est une preuve que si sa période est
indépendante et ses coûts inclus ; les périodes consultées perdent leur statut indépendant. » Trois
blocs : expériences enregistrées (plan, période, statut, essais, période indépendante), versions de
modèles et statuts de promotion, et le panneau d'**apport de JEV : A, B, C, D** alimenté par
`jev_variants` (variante, PnL net, début de période, indépendance). L'API ne publie
`validated: true` que si le rapport porte sur une période indépendante
(`src/okxq/api/routes/research.py`).

### JEV

`GET /api/v1/jev/status` et `GET /api/v1/jev/events?limit=40`. Sept tuiles : mode d'influence,
disponibilité (avec la **raison** de l'indisponibilité), âge, latence p95, taux de cache, erreurs,
dépense journalière rapportée au plafond. Puis les sources avec leur fraîcheur et leur statut, et
les événements évalués (type, mapping, version effective du modèle, âge, statut sémantique `ok` /
`late` / autre). Chacune de ces sept tuiles écrit « Non disponible » quand la valeur est `null` —
aucune ne tombe sur zéro.

### Risque et exploitation

`GET /api/v1/system/status`, `GET /api/v1/risk/events?limit=40`,
`GET /api/v1/data/quality?limit=40`. État de risque (niveau de halt, raison, perte journalière
réalisée, high-water mark, version des limites), **commandes autorisées**, suivi des demandes,
incidents de risque (sévérité, code de raison, portée, action demandée, **résultat observé**) et
qualité des données.

### Laboratoire (inerte)

Le balisage `page-labo` d'Hermes est **toujours présent** dans `index.html` mais n'est plus atteignable :
aucun bouton de navigation n'y mène et aucun script ne lie ses identifiants `lb-*`. Les quatre vues
ci-dessus l'ont remplacé. C'est du balisage conservé, pas une page livrée.

---

## 3. Les canaux de l'API

`pont.js` reproduit **exactement** la surface d'Hermes — `window.api.invoke(canal, argument)`,
`on(canal, fn)`, un flux SSE — avec trois différences imposées par la nouvelle API.

1. **Les écritures portent un jeton CSRF.** Le serveur pose un cookie `okxq_csrf` à la première
   requête authentifiée ; toute méthode non sûre le renvoie dans l'en-tête `x-csrf-token`. Sans
   cela, le serveur refuse — et il a raison : un `POST` déclenché par un autre site ne doit rien
   commander.
2. **Un 403 n'est plus une fin de partie silencieuse.** Le pont l'annonce sur le canal `pont-etat`
   pour que l'en-tête affiche « lecture seule » ou renvoie vers la porte d'accès. Le refus est une
   **réponse** (`{ok: false, error, message}`), pas une exception muette.
3. **Les routes v1 sont accessibles par `api.get(chemin)`** : les nouvelles vues lisent l'API
   versionnée plutôt qu'un canal de compatibilité.

### Surface du pont

| Appel | Effet |
|---|---|
| `api.invoke(canal, arg)` | `POST /api/<canal>` avec `{arg}` — la surface historique d'Hermes |
| `api.get(chemin, params)` | `GET` sur une route v1, `credentials: same-origin` |
| `api.post(chemin, corps)` | `POST` avec jeton CSRF |
| `api.on(canal, fn)` | Abonnement ; `« ai-log »` et `« ai:log »` désignent la même chose (les `:` sont normalisés en `-`) |
| `api.subscribe(fn)` | Raccourci sur `ai-log` |
| `api.surSante(fn)` / `api.surLien(fn)` | `health-tick` / `pont-etat` |
| `api.dernierRefus()` | Dernier refus observé |

Amorçage : une première lecture de `/api/v1/system/status` **avant** d'ouvrir le flux, parce que la
clé de l'URL doit devenir un cookie avant que l'`EventSource` parte — sinon le flux serait refusé.
Le flux `/api/flux` se reconnecte avec un recul exponentiel plafonné à 30 s.

### Canaux de compatibilité (`POST /api/<canal>`)

Servis par `src/okxq/api/routes/compat.py`, monté **en dernier** car sa route est un attrape-tout ;
un nom inconnu rend `CANAL_INCONNU`, comme l'ancien serveur. Canaux : `fetch-portfolio`,
`get-ai-state`, `ai:live-status`, `ai:training-status`, `ui-mode`, `toggle-ai`, `poser-cles`,
`chandelles`, `laboratoire`, `decisions`, `jev`, `risque`.

Deux refus explicites y portent la séparation des pouvoirs :

* `toggle-ai` **ne démarre ni n'arrête rien**. Il crée une action opérateur **auditée** (pause /
  demande de reprise) que le superviseur exécute sous préconditions. Un bouton ne contourne pas le
  Risk Engine.
* `poser-cles` **refuse**, et dit où poser les clés : dans l'environnement du seul gateway. Aucune
  clé d'échange ne se pose depuis un navigateur.

`ui-mode` rend `full` pour les rôles `operator`/`admin`, `viewer` sinon, avec
`allowLiveActivation: false` — il n'existe aucune route par laquelle le navigateur activerait LIVE.
Aucune route de compatibilité n'appelle le réseau : les chandelles viennent des **archives**, jamais
d'un appel sortant.

*Note d'honnêteté* : le texte de l'avis « lecture seule » dans `index.html` mentionne encore
`HERMES_UI_MODE=full`, hérité d'Hermes. Le mode d'interface est désormais **dérivé du rôle** par
`ui-mode` ; cette copie d'écran est périmée.

---

## 4. Trilingue FR / EN / SQ

`langues.js` tient un dictionnaire unique, trois colonnes, **324 clés**, et une règle : **tout** texte
que la page montre vient de là. C'est aussi la passe de bon français — chaque chaîne est écrite une
fois, correctement, avec ses apostrophes typographiques et ses espaces insécables, au lieu d'être
corrigée à dix endroits.

* Attributs appliqués : `data-l` (contenu), `data-l-ph` (placeholder), `data-l-title` (infobulle),
  `data-l-aria` (`aria-label`). Le HTML garde le français en dur : c'est ce qu'on voit pendant le
  chargement du script, et c'est la langue par défaut.
* Locales de formatage : `fr-FR`, `en-GB`, `sq-AL`. Tous les nombres et toutes les dates passent par
  `toLocaleString(Langues.locale())`.
* Pluriel minimal : `{s}` devient « s » au-delà de un — suffisant pour les trois langues telles
  qu'elles sont écrites.
* La langue est un réglage **d'affichage** : elle vit dans le navigateur (`localStorage`,
  clé `hermes-langue`), pas sur le serveur. Chaque lecture de stockage est gardée par un `try`.
* Un changement de langue **re-rend** la vue courante et le suivi des commandes
  (`Langues.surChangement`), sans rechargement.

**Ce qui n'est pas traduit, et c'est un choix** : le journal du moteur. Ses lignes sont la voix
technique du serveur — des données, pas de l'interface — et les traduire fabriquerait deux vérités.

---

## 5. Les quatre garde-fous visuels

### 5.1 Le jeton de mode, toujours visible

`index.html` porte dans l'en-tête un jeton permanent
`<span class="jeton jeton-mode" id="j-run-mode">`. `majEntete()` (`pages.js`) y écrit le mode lu sur
`/api/v1/system/status`, pose `data-mode="<MODE>"` (le CSS en dépend) et met le compte dans
l'infobulle : `PAPER · <compte>`. Le mode n'est pas dans un menu, ni sur une page de réglages : il
est dans l'en-tête, en permanence, à côté du nom du produit. PAPER, SHADOW, DEMO et LIVE sont
visuellement distincts.

### 5.2 Le bandeau de données synthétiques

`<div class="bandeau-synthetique" id="bandeau-synthetique" hidden>` est affiché dès que
`status.synthetic_data` est vrai, et il **reste** affiché tant que c'est le cas :

> **Données synthétiques.** Cet écran affiche un jeu de test : aucun chiffre ne provient d'un marché
> réel.

Traduit dans les trois langues (`bandeau.synth.titre`, `bandeau.synth.texte`). C'est le garde-fou le
plus important de cette interface : sans lui, une courbe d'équité synthétique se lit exactement
comme une vraie.

### 5.3 Une absence s'écrit « Non disponible », jamais zéro

Les deux fichiers de rendu définissent la même fonction avant toute autre chose :

```js
// vue.js  — Un nombre ABSENT n'est pas zéro (§59)
const ND = () => t("indispo");
// pages.js — Un nombre absent reste absent.
const ND = () => `<span class="indispo">${ech(t("indispo"))}</span>`;
```

`num()`, `pct()`, `txt()` et `quand()` rendent `ND()` pour `null`, `undefined`, chaîne vide ou
`NaN` — et `ND()` rend « Non disponible » / « Not available » / « E padisponueshme ». La raison est
opérationnelle : **un zéro se lit comme une mesure**, et confondre une absence avec 0 % de taux de
gain ou 0 USDT de perte fait prendre des décisions sur du vide. Une équité inconnue affichée à zéro
se lirait comme un compte vidé.

Côté serveur, la même règle est tenue : `_f()` dans `compat.py` laisse `None` à `None` plutôt que de
le convertir en `0.0`, et `_performance()` rend `None` quand il n'y a eu aucun trade — jamais 0 %.
Les compteurs qui ne vivent que dans la mémoire du worker JEV (`cache_hit_ratio`,
`daily_spend_usd`) sont relayés depuis son instantané ; sans worker en marche, ils restent
« non disponibles ».

### 5.4 Toute commande critique exige une confirmation montrant le compte et le mode

Les quatre commandes de la page Risque — `pause`, `cancel-entry-orders`, `request-flatten`,
`request-resume` — passent par `commander()`, qui appelle d'abord `confirmer()` :

```js
/* Confirmation contextuelle : le compte ET le mode sont sous les yeux avant d'agir. */
```

La boîte (`role="dialog" aria-modal="true"`) affiche le libellé de l'action, puis **`compte : <scope>`
et `mode : <MODE>`** lus dans le contexte de la page, puis deux boutons. Un clic hors de la boîte ou
sur « non » annule.

Le corps de la requête porte `reason`, `scope`, `actor` et un `request_id` **idempotent** dérivé de la
minute courante : l'API refuse une commande sans motif, et un double-clic ne crée pas deux demandes.

Ensuite, et c'est le point le plus important : **une demande acceptée n'est pas une action
effectuée**. `suivre(requestId)` interroge `GET /api/v1/control/requests/<id>` et **continue** tant
que le statut vaut `REQUESTED` ou `PENDING` (relance toutes les 5 s). Le tableau de suivi montre la
demande, l'action, le statut (`APPLIED` / `REFUSED` / `FAILED` / en cours), le **résultat observé** et
le nombre de **résidus** d'exposition. Le suivi ne disparaît pas parce que le serveur a répondu
`200`. La note sous les boutons le dit dans les trois langues.

Un refus n'est pas silencieux : il entre dans le tableau de suivi avec le statut `REFUSED` et le
message du serveur.

---

## 6. Sécurité côté page

* Aucun secret n'est stocké côté navigateur : la clé présentée dans l'URL devient une **session
  signée** côté serveur (cookie `HttpOnly`) et l'URL est nettoyée par une redirection 303.
* `localStorage` ne sert qu'à des préférences d'affichage (`hermes-langue`, `okxq-page`), et chaque
  accès est gardé.
* Tout texte injecté dans le DOM par `pages.js` passe par `ech()` (échappement de `& < > " '`).
* Aucun appel à un CDN : polices et scripts sont servis depuis `frontend/`.
* Aucun formulaire ne permet de saisir une clé d'échange (le canal `poser-cles` refuse).

---

## 7. Le contrat UI/API est verrouillé, et vérifié dans un vrai navigateur

### `tests/contract/test_ui_api_contract.py` — sans navigateur

Ce test existe à cause d'un défaut réel : l'API publiait le bloc de risque sous `halts` tandis que
`pages.js` lisait `risk`. **Ce n'est pas une erreur Python** — la page continuait de s'afficher, et
chaque valeur devenait simplement « Non disponible ». C'est le pire genre de défaut : silencieux et
indistinguable d'une absence de données.

Le test verrouille les **deux** côtés :

1. pour chaque endpoint appelé par l'interface, l'API répond `200` avec les clés attendues —
   `/api/v1/system/status` (`ok`, `mode`, `account_scope`, `synthetic_data`, `halts`),
   `/api/v1/decisions`, `/api/v1/experiments` (`items`, `jev_variants`), `/api/v1/models`,
   `/api/v1/jev/status` (les dix clés que lisent les sept tuiles), `/api/v1/jev/events`,
   `/api/v1/risk/events`, `/api/v1/data/quality` ;
2. le bloc imbriqué `halts` porte bien ses cinq champs, et la **fixture sème réellement** les lignes
   nécessaires (`RiskState`, `ExperimentRun`, `EvaluationReport`) — sans quoi le bloc vaudrait `null`
   et le test se contenterait de sauter. Un test sauté n'est pas un test qui passe ;
3. `pages.js` cite bien ces mêmes noms : chaque endpoint doit apparaître dans la source, et
   l'interface doit lire `etat.halts` — sinon le bloc a été renommé d'un seul côté.

Il tourne donc aussi là où aucun Chromium n'est disponible.

### `tests/e2e/test_ui_smoke.py` — dans un vrai navigateur

Ce test répond à des questions qu'aucun test Python ne peut trancher. Il démarre l'API sur un port
libre avec une base SQLite semée, ouvre la page dans **Chromium** (Playwright), collecte les erreurs
de page et de console, puis vérifie :

* le mode est visible en permanence (`#t-run-mode` vaut `PAPER`, `#j-run-mode[data-mode]` vaut
  `PAPER`) et le **bandeau de données synthétiques est affiché** ;
* les cinq onglets existent ; la page Décisions se rend avec ses lignes (`NO_TRADE` présent,
  `EDGE_BELOW_COSTS` dans les rejets) ;
* une métrique inconnue s'écrit bien « **Non disponible** » sur la page JEV ;
* la page Risque montre l'état (`NONE`, `DATA_STALE`) et un clic sur « Demander une sortie » ouvre
  une confirmation dont le texte contient **le compte (`smoke-fixture`) ET le mode (`PAPER`)** ;
  « non » la referme sans effet ;
* la bascule de langue re-rend la page : `Décisions` → `Decisions` → `Vendimet` ;
* **aucune erreur JavaScript** n'a été émise (`assert errors == []`).

Il saute proprement, **avec son motif**, si aucun Chromium utilisable n'est trouvé : un test non
exécuté reste NOT_RUN, jamais PASS. L'environnement fournit un Chromium préinstallé dont la révision
peut différer de celle que la version épinglée de Playwright irait télécharger ; le test lance donc
le binaire préinstallé (`PLAYWRIGHT_BROWSERS_PATH`, `--no-sandbox`) sans aucun réseau.

**Résultat réellement observé dans cette session** : ce smoke navigateur a été **exécuté et est
vert** — `tests.e2e.test_ui_smoke::test_interface_loads_without_js_errors_and_shows_mode`, environ
2,8 s, présent dans `reports/junit.xml` **sans balise `skipped`**. C'est le point qui compte : le
test n'a pas été sauté, une page a réellement été chargée dans Chromium. Le décompte courant de la
suite est dans `docs/test_matrix.md`, qui est régénéré depuis ce même rapport.

### `frontend/tests/interface.test.js` — 8 tests `node --test`

Ils vérifient ce qui casse en silence : chaque clé de traduction existe dans les **trois** langues ;
les clés des nouvelles vues sont traduites ; le pont envoie le jeton CSRF sur les écritures et expose
`get`/`post`/`invoke` ; les canaux appelés par la page existent dans la couche de compatibilité ; une
valeur absente s'écrit « Non disponible », jamais zéro ; le mode et la nature des données sont
visibles dans la page ; les commandes critiques demandent une confirmation montrant compte et mode ;
tous les scripts de la page sont syntaxiquement valides.

Ces tests ne sont **pas** dans la suite pytest : ils s'exécutent par `make ui-test`
(`node --test "frontend/tests/*.test.js"`), donc ils n'apparaissent pas dans `reports/junit.xml` ni
dans `docs/test_matrix.md`.

---

## 8. Limites assumées

* **T65 (action UI non autorisée / CSRF : aucun effet + événement d'audit) est `NOT_RUN`.** Le
  mécanisme est écrit et détaillé dans `src/okxq/api/auth.py`, mais aucun test du dépôt ne porte cet
  identifiant : les tests d'interface présentent toujours une clé **valide**. Le refus, le CSRF et
  l'audit du refus ne sont donc pas vérifiés. Voir `docs/threat_model.md` § M9.
* Les tests frontend `node --test` ne sont pas comptés dans la matrice §64 (voir ci-dessus).
* Le balisage `page-labo` est conservé mais inerte (§ 2).
* L'avis « lecture seule » porte un texte hérité (`HERMES_UI_MODE=full`) qui ne correspond plus au
  mécanisme de rôles.
* L'interface est déclarée « lisible sur ordinateur et tablette » par le cahier des charges ; aucune
  mesure d'accessibilité automatisée (contraste, navigation clavier complète, lecteur d'écran) n'a
  été exécutée. Les contrastes cités dans `vue.js` viennent d'un validateur, mais ce validateur n'est
  pas rejoué par un test du dépôt.
* Aucune donnée réelle n'a jamais été affichée par cette interface : uniquement des fixtures et des
  jeux synthétiques. Tout chiffre visible sur ces écrans mesure le pipeline, pas le marché.
