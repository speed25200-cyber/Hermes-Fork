# Rapport de livraison — plateforme quantitative OKX + JEV

Ce document dit ce qui existe, ce qui est vérifié, et ce qui ne l'est pas. Il n'affirme aucun
avantage de marché, et aucune ligne ici n'autorise un passage en argent réel.

Date : 18 septembre 2026. Dépôt : `speed25200-cyber/Hermes-Fork`, branche
`claude/hermes-fork-migration-m4wcbm`.

## 1. En une phrase

La plateforme est construite, testée hors ligne — schéma et migrations compris, sur un vrai
PostgreSQL 16 — et déployable ; elle n'a **jamais** parlé à OKX ni à TypeSafe dans cette session,
LIVE reste désactivé par conception, et l'interface graphique de Hermes est conservée puis étendue.

## 2. Vérifications réellement exécutées

| Contrôle | Commande | Résultat |
| --- | --- | --- |
| Suite complète | `pytest -q -p no:randomly` | **825 verts**, 0 échec, **0 sauté** |
| Schéma et migrations sur PostgreSQL 16 | `pytest tests/integration` avec `OKXQ_TEST_DATABASE_URL` | **8 verts contre un serveur réel** |
| Matrice §64 | `scripts/test_matrix_status.py` | **70 / 70 identifiants PASS**, 0 NOT_RUN, **0 FAIL** |
| Lint et format | `ruff check --no-cache` + `ruff format --check` | propres sur 230 fichiers |
| Types | `mypy` (strict, `src/okxq`) | propre sur 156 modules |
| Interface (navigateur) | `pytest tests/e2e/test_ui_smoke.py -m e2e` | **1 vert** dans Chromium, 0 erreur JavaScript |
| Interface (dictionnaire, contrats) | `node --test "frontend/tests/*.test.js"` | 8 verts |
| Contrat interface ↔ API et contrats fournisseurs | `pytest tests/contract -m contract` | 39 verts, sans navigateur ni réseau |
| Commandes de `compose.yaml` | `pytest tests/unit/test_compose_commands.py` | analysées par la vraie CLI |
| Parcours complet hors ligne | `make smoke-offline` | 21 contrôles, 4 régimes × 2 scénarios, reproductibilité bit à bit |
| Sécurité du dépôt | `scripts/security_check.py` | aucune anomalie sur les fichiers indexés |
| Scripts de déploiement | `bash -n` sur `deploy/*.sh`, `infra/*.sh` | syntaxe valide |
| Compose | `docker compose config` | valide ; séparation des secrets prouvée service par service |
| **Intégration continue** | workflow `ci.yml` sur GitHub | **verte** : lint, format, types, contrôles de sécurité, tests hermétiques, parcours hors ligne, tests d'interface |

## 3. Ce qui n'a PAS été exécuté, et pourquoi

Un test non exécuté reste **NOT_RUN**. Il ne devient pas vert parce que le reste de la suite l'est.

| Sujet | Statut | Raison |
| --- | --- | --- |
| Connecteur privé OKX (DEMO et LIVE) | NOT_RUN | aucune clé OKX dans cette session |
| Appel réel au composant sémantique JEV | NOT_RUN | aucune clé TypeSafe dans cette session |
| Collecte de marché publique | NOT_RUN | la politique réseau de l'environnement refuse le CONNECT vers OKX (403, y compris vers des hôtes tiers) |
| Schéma sur PostgreSQL | **EXÉCUTÉ** | un serveur PostgreSQL 16 a été lancé localement ; les 8 tests `integration` passent réellement (TIMESTAMPTZ et JSONB partout, précisions NUMERIC, clés séquentielles, unicité durable du `client_order_id`, CHECK de positivité, migration réversible puis rejouable) |
| Construction de l'image Docker | NOT_RUN | le démon Docker ne tourne pas ; seul le client a validé `compose.yaml` |
| Recherche sur historique réel | NOT_RUN | aucun historique de marché réel ; seules des données synthétiques ont servi |

Les 70 identifiants de la matrice sont désormais adossés à au moins un test exécuté. **Cela ne
signifie pas que la plateforme est validée.** Un `PASS` dit « ce comportement est vérifié sur
fixtures hors ligne » ; la suite ne contient **aucun** test `connected`, et `docs/test_matrix.md`
le déclare en tête de document plutôt que de laisser lire « 70/70 » comme un feu vert.

Deux identifiants méritent d'être nommés, parce qu'ils portent sur de l'argent réel :

- **T63** (« un échec DEMO ne bascule jamais vers LIVE ») est couvert par
  `tests/unit/test_demo_never_falls_back_to_live.py`, qui fait échouer l'échange de cinq façons
  différentes et vérifie après chacune que l'en-tête `x-simulated-trading: 1` est toujours là, que
  le domaine visé reste celui du profil, et que `demo` n'est pas un drapeau modifiable. L'assertion
  est écrite **en toutes lettres** et non reprise de la constante du code : une première version
  comparait le code à lui-même et survivait à une mutation qui aurait envoyé de vrais ordres.
- **T64** (« LIVE sans approbations complètes ») refuse avant toute connexion privée. Il n'existe
  aucune option de contournement, et aucune commande `--force`.

## 3bis. Dépôt public tenant des secrets de déploiement

`speed25200-cyber/Hermes-Fork` est un dépôt **PUBLIC**, et c'est là que vivent désormais
`VPS_PASSWORD` et `TYPESAFE_API_KEY`. Ce qu'un dépôt public change, précisément :

**Ce qui reste protégé.** Les valeurs des secrets Actions ne sont pas lisibles, même sur un dépôt
public. Une *pull request* venue d'un fork ne reçoit **aucun** secret — c'est la règle GitHub par
défaut, et c'est le principal vecteur d'exfiltration. Les trois workflows respectent les conditions
qui la rendent effective :

| Workflow | Déclencheurs | Secrets |
| --- | --- | --- |
| `ci.yml` | `push`, `pull_request`, `workflow_dispatch` | **aucun** |
| `deploy-vps.yml` | `workflow_dispatch` **seulement** | `VPS_PASSWORD`, `TYPESAFE_API_KEY`, `OKXQ_OPERATOR_KEY` |
| `vps-status.yml` | `workflow_dispatch` **seulement** | idem + clés OKX |

Aucun `pull_request_target`, aucun `workflow_run`, aucun `issue_comment` : ce sont les déclencheurs
qui donnent des secrets à du code venu de l'extérieur. Les deux workflows sensibles n'existent qu'en
lancement manuel, ce qui exige un droit d'écriture sur le dépôt.

**Ce qui était exposé, et ne l'est plus.** `deploy-vps.yml` et `vps-status.yml` acceptaient une
entrée `root_password`, et `deploy-vps.yml` une entrée `jeton`. La valeur saisie dans une entrée
`workflow_dispatch` est enregistrée dans la charge utile de l'événement et **affichée sur la page du
run** — publique pour un dépôt public. `::add-mask::` masque la sortie des étapes, **pas** cette
page. Taper le mot de passe root là aurait donc suffi à le publier mondialement. Les deux entrées
sont **supprimées** : le mot de passe ne vient plus que du magasin de secrets, et la clé opérateur
d'un secret facultatif `OKXQ_OPERATOR_KEY`.

**Ce qui reste visible et ne peut pas l'être moins.** La conception complète : architecture, limites
de risque, politique d'exécution, et `docs/threat_model.md` qui décrit les défenses et leurs
limites. Ce n'est pas une fuite — un système dont la sécurité dépend du secret de sa conception n'en
a pas. Mais c'est une décision qui vous appartient, et elle n'est pas évidente en regardant le
dépôt.

Si vous passiez `Hermes-Fork` en privé, rien ne casserait côté déploiement (les workflows y vivent
et s'y exécutent). La seule chose qui casserait est le workflow `migrer-vers-okxq.yml` du dépôt
`Hermes`, qui récupère la plateforme sans jeton **parce qu'elle est publique** ; il faudrait lui
passer un `token:`. C'est écrit dans son commentaire et dans `MIGRATION.md`.

## 4. Aucune preuve d'avantage de marché

Le seul chiffrage de performance produit dans cette session est `reports/smoke-offline.json`. Il
montre un résultat de stratégie **négatif dans tous les scénarios comportant une exécution**, du
montant des frais. C'est le comportement attendu : un aller-retour sur un marché plat coûte de
l'argent, et c'est précisément ce que le contrôle vérifie.

Les travaux de recherche ont tourné sur des données synthétiques (marche aléatoire à facteur commun,
sans signal planté). Sur du bruit, le signe d'un écart entre variantes est lui-même aléatoire. Aucun
modèle n'est promouvable, aucune calibration n'est validée, et le seul statut atteignable est
`CANDIDATE`.

## 5. Sécurité — ce qui est tenu par construction

| Principe | Comment il est tenu | Où |
| --- | --- | --- |
| Aucun secret dans Git | motifs ancrés `/env/`, `/backups/`, `.env*` ; contrôle automatique qui refuse un secret indexé | `.gitignore`, `scripts/security_check.py` |
| Séparation des secrets par service | un `env_file` distinct par service, aucun bloc d'environnement partagé ; vérifié en comparant l'environnement résolu des 8 services | `compose.yaml`, `infra/env/*.example` |
| Seul le gateway signe | un rôle non autorisé **refuse de démarrer** si des identifiants d'échange sont dans son environnement | `runtime/composition.py` |
| JEV ne voit jamais les identifiants d'échange | `TYPESAFE_API_KEY` n'existe que dans `env/jev-worker.env` | `compose.yaml` |
| L'apprentissage ne contourne pas le risque | le prédicteur n'importe ni intention ni ordre approuvé (vérifié par analyse syntaxique, pas textuelle) | `research/predictor.py` |
| Masquage inconditionnel des secrets | masquage en dernier ressort avant rendu, et niveau des loggers tiers remis à NOTSET pour qu'aucun tiers ne puisse rendre le journal muet | `runtime/logging.py` |
| Action d'interface non autorisée sans effet | rôle, session signée, double soumission CSRF, cookie forgé : aucune demande actionnable écrite, refus audité | `api/auth.py`, tests T65 |
| LIVE désactivé par défaut | refus si `live_enabled` est faux ; manifeste signé exigé ; **aucune option de contournement** | `config/live_guard.py`, `runtime/composition.py` |
| Clés jamais affichées | `okxq api keys` écrit un fichier 0600 et n'affiche que des empreintes tronquées | `cli_cmds/api_cmd.py` |

## 6. Interface graphique conservée

L'interface de Hermes est reprise telle quelle — mêmes jetons de couleur, mêmes composants, même
trilingue FR/EN/SQ — puis étendue de quatre vues (Décisions, Recherche, JEV, Risque). Trois règles la
gouvernent :

- une valeur absente s'écrit « Non disponible », jamais zéro ;
- le mode d'exécution est visible en permanence, et un bandeau signale des données synthétiques ;
- toute commande critique exige une confirmation montrant le compte ET le mode.

Le contrat avec l'API est verrouillé des deux côtés par `tests/contract/test_ui_api_contract.py`
(sans navigateur) et vérifié dans Chromium par `tests/e2e/test_ui_smoke.py`.

## 7. Défauts trouvés et corrigés pendant la construction

Le détail, avec l'effet observable et la correction, est dans `BUILD_STATUS.md`. Les plus
significatifs, en commençant par les plus graves :

1. **La protection automatique ne l'était pas.** Rien dans le runtime n'appelait
   `kill_switch.observe()`. Le kill switch était seulement *lu* (`rt.kill_switch.level`) et jamais
   alimenté : la frontière de jour UTC n'était jamais franchie, la perte journalière jamais
   calculée, le sommet d'équité jamais mis à jour, et **aucun déclencheur ne pouvait se produire**.
   La principale protection de la plateforme ne s'activait que sur commande humaine. Elle est
   maintenant évaluée à chaque frontière, **avant** la décision — constater le halt après aurait
   laissé passer exactement une décision de trop.

2. **Le compte papier n'avait pas d'argent au grand livre.** `VirtualExchange` recevait bien
   `initial_cash`, mais le grand livre — seule source d'equity du système — démarrait à zéro. Donc
   `day_start_equity = 0`, donc `daily_loss_fraction` rendait `None`, et le Risk Engine **saute** le
   contrôle dont la mesure est absente : `if ctx.daily_loss_fraction is not None and ...`. La limite
   de perte journalière était inatteignable en PAPER, c'est-à-dire dans le seul mode que la
   plateforme est autorisée à faire tourner. Deux autres mesures tombaient avec elle (sommet
   d'équité, valeur de part). Ces trois défauts se masquaient l'un l'autre : chacun rendait `None`,
   et `None` ne ressemble pas à une panne — il ressemble à « pas encore mesuré ».

3. **Un dépôt effaçait la perte du jour.** L'equity remontait au-dessus de son point de départ,
   donc la perte journalière retombait à zéro : il suffisait d'un virement pour faire disparaître
   une limite atteinte et rendre une reprise possible le jour même. La perte du jour est désormais
   un plafond monotone à l'intérieur du jour UTC (T45).

4. **Les réponses JEV d'un actif servaient de features à un autre.** Sur un document
   multi-actifs, le rapprochement point-in-time acceptait toute évaluation dont l'actif figurait
   dans la cartographie du document — donc l'évaluation de BTC était servie comme feature d'ETH, et
   réciproquement. Or `evaluate_all` fait un appel *par actif* précisément parce que les réponses
   diffèrent d'un actif à l'autre pour le même texte.

5. Une exclusion Git non ancrée (`data/`, `runtime/`) masquait **seize modules** de la couche de
   données et du runtime : ils fonctionnaient sur le disque local sans avoir jamais été versionnés.

6. Deux contrôles de causalité point-in-time étaient inopérants — l'un testait le mauvais champ,
   l'autre avait un corps vide (`pass`).

7. Le relevé de migration VPS écrivait `0 position ouverte` quand il n'avait **pas pu lire** l'état,
   juste avant un effacement irréversible. Il distingue désormais un nombre d'un état `inconnu`, et
   le workflow refuse sur un inconnu.

8. Un refus de rôle n'était pas audité faute de piste d'audit attachée à l'application : un lecteur
   authentifié sondant une commande privilégiée ne laissait aucune trace, là où un anonyme en
   laissait une.

9. Le journal comptable écrivait ses lignes avant leur transaction parente, et toutes les écritures
   échouaient en silence.

10. Une réservation de profondeur était libérée trop tôt, permettant à deux ordres de consommer la
   même liquidité affichée.

11. Le service de migration de `compose.yaml` portait `okxq db upgrade head`. `head` est une option,
   pas un positionnel : la CLI rejetait la commande, donc la migration échouait à chaque démarrage.
   Comme les cinq rôles écrivains attendent sa terminaison réussie, **aucun ne démarrait jamais** —
   seule l'API montait, donnant une console vivante devant un système mort. Une erreur d'une ligne,
   invisible sans déployer. Un test soumet désormais chaque commande de `compose.yaml` à l'analyseur
   d'arguments de la vraie CLI ; deux versions antérieures de ce test étaient elles-mêmes vacuoles
   (l'aide court-circuite l'analyse ; Typer embarque sa propre copie de Click, ce qui rendait les
   tests de type toujours faux) et le commentaire du test le documente.

12. Le chemin de prix des labels était échantillonné sur l'horodatage brut du premier événement,
   décalé de la latence d'ingestion, alors que les décisions sont alignées sur la minute. Aucun point
   ne tombait dans la fenêtre d'entrée et **100 % des labels sortaient NO_ENTRY** : le jeu
   d'entraînement était vide sans que rien ne le signale, ce qui est plus dangereux qu'une erreur —
   l'entraînement « réussit » et le modèle n'a rien appris.

13. Le contrôle local de lint rendait un verdict PÉRIMÉ. La CI refusait deux fichiers que
   `ruff check` déclarait propres, avec la même version et la même commande : c'était le cache de
   ruff. J'avais rapporté « ruff propre » sur la foi d'un résultat mis en cache. `make lint` passe
   désormais `--no-cache` et `make typecheck` `--no-incremental` : un garde-fou qui affirme le
   contraire de la vérité est pire que pas de garde-fou.

14. Trois documents de contrat fournisseur référencés par `docs/api_contracts.md` et par la matrice
   des exigences (§46) **n'existaient pas**. Un contrat manquant se remarque ; un contrat périmé,
   non — il garde l'autorité d'un document tout en décrivant un autre système. Les trois sont
   désormais *générés* depuis le code et le manifeste de capacités, et un test échoue si l'un dérive
   de sa source.

Chaque correctif a été vérifié en le retirant : les tests correspondants échouent alors.

**Deux de mes propres tests étaient creux** et je les ai trouvés en les mutant, pas en les relisant.
L'un comparait l'en-tête `x-simulated-trading` à la constante du code : renommer la constante en
`x-simulated-trading-DISABLED` laissait le test vert, alors que de vrais ordres seraient partis.
L'autre comparait un domaine de région à lui-même. Les deux sont réécrits avec la valeur attendue
**en toutes lettres**, et `tests/unit/test_no_hollow_tests.py` interdit désormais ce motif. Un test
qui compare le code à lui-même est pire qu'un test absent : il rassure.

## 8. Migration VPS — prête, non déclenchée

Le workflow `deploy-vps.yml` est dispatchable et porte trois gardes :

1. `confirmer` doit valoir exactement `EFFACER` ;
2. le relevé préalable refuse s'il trouve des positions ouvertes, sauf `positions_ouvertes=accepter` ;
3. il refuse aussi s'il n'a **pas pu déterminer** l'état des positions, sauf
   `positions_inconnues=accepter`.

**Elle n'a pas été déclenchée, et elle ne peut pas l'être depuis ici** : la politique de sortie
réseau de cette session refuse toute connexion vers la machine. Le déclenchement passe donc par
GitHub Actions, depuis le dépôt `Hermes` où vivent les secrets.

### Marche à suivre

1. **Enregistrer deux secrets de dépôt** dans `speed25200-cyber/Hermes`
   (*Settings → Secrets and variables → Actions*) :
   - `VPS_PASSWORD` — le mot de passe root de la machine ;
   - `TYPESAFE_API_KEY` — la clé JEV.

   Ils ne doivent apparaître **ni dans le code, ni dans une entrée `workflow_dispatch`** : une entrée
   de dispatch reste affichée dans la page du run et dans ses métadonnées, donc l'y coller
   reviendrait à la publier. Le workflow les fait voyager par l'entrée standard, jamais par la ligne
   de commande — les arguments d'un processus sont lisibles par tout utilisateur de la machine et
   finissent dans l'historique du shell distant.

2. **Dispatcher `migrer-vers-okxq.yml` avec `migrer=false`.** Sur une machine neuve il n'y a rien à
   effacer : n'employez `migrer=true` / `confirmer=EFFACER` que sur un serveur portant réellement
   l'ancien Hermes, et après avoir vérifié **directement sur OKX** qu'aucune position n'est ouverte.
   Un moteur arrêté ne ferme aucune position ; elles restent ouvertes chez OKX, sans surveillance.

3. **Vérifier après coup** : l'interface répond sur le port 8899, et une réponse `403` sur `/` sans
   clé est le **bon** comportement. `/health/ready` peut renvoyer `503` au premier démarrage, le
   temps que la réconciliation et les données soient prêtes.

### Sur les identifiants transmis en conversation

Le mot de passe root de la machine et la clé TypeSafe m'ont été communiqués dans le fil de
discussion. Je ne les ai écrits nulle part — ni dans un fichier, ni dans un commit, ni dans un
journal — et `scripts/security_check.py` surveille désormais les quatre noms qui les porteraient
(`VPS_PASSWORD`, `ROOT_PASSWORD`, `SSH_PASSWORD`, `SSHPASS`).

Cela dit : **un secret qui a transité par une conversation doit être considéré comme exposé.** La
recommandation, dans l'ordre :

1. changer le mot de passe root de la machine, puis n'y accéder que par clé SSH ;
2. faire tourner la clé TypeSafe depuis la console du fournisseur une fois la plateforme installée ;
3. n'enregistrer les nouvelles valeurs que dans les secrets Actions du dépôt.

Ce n'est pas une formalité : le mot de passe de la machine est le secret le **plus puissant** du
déploiement, puisqu'il donne le serveur entier, et donc tous les secrets qu'il porte.

## 9. Passage en argent réel — non autorisé

Aucun gate de §71 n'est franchi : pas de dossier prospectif, pas de période SHADOW mesurée, pas de
validation technique DEMO, aucun manifeste d'approbation signé. `docs/live_readiness.md` tient le
compte. La mécanique de garde est implémentée et testée ; son verdict actuel est **refus**, et c'est
le verdict correct.

Conformément à la consigne : **aucun passage en argent réel sans validation et autorisation
explicites**.

## 10. Où regarder ensuite

| Question | Fichier |
| --- | --- |
| Où en est chaque phase, quels défauts connus | `BUILD_STATUS.md` |
| Quel test couvre quelle exigence | `docs/test_matrix.md` |
| Que signifie chaque champ en base | `docs/data_dictionary.md` |
| Quelles menaces sont couvertes, lesquelles non | `docs/threat_model.md` |
| Comment démarrer, arrêter, sauvegarder, tourner les clés | `docs/runbooks/` |
| Pourquoi ces choix de structure | `docs/adr/` |
| Méthode de recherche et absence d'alpha | `docs/strategy_research.md` |
| Interface : pages, canaux, règles d'affichage | `docs/ui.md` |
