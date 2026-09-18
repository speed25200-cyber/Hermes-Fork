# Rapport de livraison — plateforme quantitative OKX + JEV

Ce document dit ce qui existe, ce qui est vérifié, et ce qui ne l'est pas. Il n'affirme aucun
avantage de marché, et aucune ligne ici n'autorise un passage en argent réel.

Date : 18 septembre 2026. Dépôt : `speed25200-cyber/Hermes-Fork`, branche
`claude/hermes-fork-migration-m4wcbm`.

## 1. En une phrase

La plateforme est construite, testée hors ligne et déployable ; elle n'a jamais parlé à OKX ni à
TypeSafe dans cette session, LIVE reste désactivé par conception, et l'interface graphique de Hermes
est conservée puis étendue.

## 2. Vérifications réellement exécutées

| Contrôle | Commande | Résultat |
| --- | --- | --- |
| Tests hermétiques | `pytest -m "not integration and not connected"` | **507 verts**, 0 échec, 0 sauté |
| Matrice §64 | `scripts/test_matrix_status.py` | **48 / 70 identifiants PASS**, 22 NOT_RUN, **0 FAIL** |
| Lint et format | `ruff check` + `ruff format --check` | propres sur 211 fichiers |
| Types | `mypy` (strict, `src/okxq`) | propre sur 156 modules |
| Interface (navigateur) | `pytest tests/e2e/test_ui_smoke.py -m e2e` | **1 vert** dans Chromium, 0 erreur JavaScript |
| Interface (dictionnaire, contrats) | `node --test "frontend/tests/*.test.js"` | 8 verts |
| Contrat interface ↔ API | `pytest tests/contract -m contract` | 11 verts, sans navigateur |
| Commandes de `compose.yaml` | `pytest tests/unit/test_compose_commands.py` | analysées par la vraie CLI |
| Parcours complet hors ligne | `make smoke-offline` | 21 contrôles, 4 régimes × 2 scénarios, reproductibilité bit à bit |
| Sécurité du dépôt | `scripts/security_check.py` | aucune anomalie sur les fichiers indexés |
| Scripts de déploiement | `bash -n` sur `deploy/*.sh`, `infra/*.sh` | syntaxe valide |
| Compose | `docker compose config` | valide ; séparation des secrets prouvée service par service |

## 3. Ce qui n'a PAS été exécuté, et pourquoi

Un test non exécuté reste **NOT_RUN**. Il ne devient pas vert parce que le reste de la suite l'est.

| Sujet | Statut | Raison |
| --- | --- | --- |
| Connecteur privé OKX (DEMO et LIVE) | NOT_RUN | aucune clé OKX dans cette session |
| Appel réel au composant sémantique JEV | NOT_RUN | aucune clé TypeSafe dans cette session |
| Collecte de marché publique | NOT_RUN | la politique réseau de l'environnement refuse le CONNECT vers OKX (403, y compris vers des hôtes tiers) |
| Schéma sur PostgreSQL | NOT_RUN | aucun serveur PostgreSQL disponible ; les 8 tests `integration` sautent proprement |
| Construction de l'image Docker | NOT_RUN | le démon Docker ne tourne pas ; seul le client a validé `compose.yaml` |
| Recherche sur historique réel | NOT_RUN | aucun historique de marché réel ; seules des données synthétiques ont servi |

Les 22 identifiants encore NOT_RUN de la matrice (T23–T26, T36, T37, T40–T43, T45–T48, T50, T51,
T57, T59, T61, T63, T67, T68) sont listés sans être masqués dans `docs/test_matrix.md`. Pour
plusieurs d'entre eux l'assertion existe dans le code sans qu'un test porte l'identifiant ; pour les
autres, l'accès manquant est la cause.

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
significatifs :

1. Une exclusion Git non ancrée (`data/`, `runtime/`) masquait **seize modules** de la couche de
   données et du runtime : ils fonctionnaient sur le disque local sans avoir jamais été versionnés.
2. Deux contrôles de causalité point-in-time étaient inopérants — l'un testait le mauvais champ,
   l'autre avait un corps vide (`pass`).
3. Le relevé de migration VPS écrivait `0 position ouverte` quand il n'avait **pas pu lire** l'état,
   juste avant un effacement irréversible. Il distingue désormais un nombre d'un état `inconnu`, et
   le workflow refuse sur un inconnu.
4. Un refus de rôle n'était pas audité faute de piste d'audit attachée à l'application : un lecteur
   authentifié sondant une commande privilégiée ne laissait aucune trace, là où un anonyme en
   laissait une.
5. Le journal comptable écrivait ses lignes avant leur transaction parente, et toutes les écritures
   échouaient en silence.
6. Une réservation de profondeur était libérée trop tôt, permettant à deux ordres de consommer la
   même liquidité affichée.
7. Le service de migration de `compose.yaml` portait `okxq db upgrade head`. `head` est une option,
   pas un positionnel : la CLI rejetait la commande, donc la migration échouait à chaque démarrage.
   Comme les cinq rôles écrivains attendent sa terminaison réussie, **aucun ne démarrait jamais** —
   seule l'API montait, donnant une console vivante devant un système mort. Une erreur d'une ligne,
   invisible sans déployer. Un test soumet désormais chaque commande de `compose.yaml` à l'analyseur
   d'arguments de la vraie CLI ; deux versions antérieures de ce test étaient elles-mêmes vacuoles
   (l'aide court-circuite l'analyse ; Typer embarque sa propre copie de Click, ce qui rendait les
   tests de type toujours faux) et le commentaire du test le documente.
8. Le chemin de prix des labels était échantillonné sur l'horodatage brut du premier événement,
   décalé de la latence d'ingestion, alors que les décisions sont alignées sur la minute. Aucun point
   ne tombait dans la fenêtre d'entrée et **100 % des labels sortaient NO_ENTRY** : le jeu
   d'entraînement était vide sans que rien ne le signale, ce qui est plus dangereux qu'une erreur —
   l'entraînement « réussit » et le modèle n'a rien appris.

Chaque correctif a été vérifié en le retirant : les tests correspondants échouent alors.

## 8. Migration VPS — prête, non déclenchée

Le workflow `deploy-vps.yml` est dispatchable et porte trois gardes :

1. `confirmer` doit valoir exactement `EFFACER` ;
2. le relevé préalable refuse s'il trouve des positions ouvertes, sauf `positions_ouvertes=accepter` ;
3. il refuse aussi s'il n'a **pas pu déterminer** l'état des positions, sauf
   `positions_inconnues=accepter`.

**Elle n'a pas été déclenchée.** Deux raisons, toutes deux à trancher par l'opérateur :

- le secret `VPS_PASSWORD` vit dans les secrets Actions du dépôt `Hermes` et n'est pas accessible
  depuis cette session ;
- l'effacement est irréversible et je ne peux vérifier d'ici ni l'état de l'ancien moteur ni la
  présence de positions réelles sur le compte OKX. Lancer un effacement dans ces conditions serait
  exactement ce que la troisième garde existe pour empêcher.

Marche à suivre : vérifier sur OKX qu'aucune position n'est ouverte, puis dispatcher le workflow avec
`migrer=true` et `confirmer=EFFACER`. Le profil par défaut est `paper`.

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
