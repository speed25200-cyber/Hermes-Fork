# Audit Hermes Fork — 30 septembre 2026

## Version et observations

L'installation active provient de `claude/trading-ai-sota-rebuild-s3qyg3`, commit
`22facd44104a7994cc3365f0696898da4c98d789`. La branche `main` porte encore un autre moteur (`okxq`) :
les correctifs de cet audit partent donc de la branche effectivement déployée (`hermes`).

Le job [VPS status, tentative actualisée](https://github.com/speed25200-cyber/Hermes-Fork/actions/runs/36106193954)
a été relancé pour lire l'état du serveur, sans relancer le trading ni déployer de code.
L'instantané du moteur est daté du **30 septembre à 10:00:12 UTC**, bougie 09:30 UTC.
Il concerne le **compte papier**, pas un compte d'argent réel.

| Mesure | Valeur observée |
|---|---:|
| Capital initial déclaré dans la configuration | 10 000 USDT |
| Équité | 9 364,71 USDT |
| PnL net simulé | −635,29 USDT (−6,35 %) |
| Frais cumulés | 18,87 USDT |
| Funding net encaissé | +10,68 USDT |
| Résidu prix et exécution, hors frais et funding | −627,10 USDT |
| Exposition brute / nette | 0,8203 / 0,0768 |
| IC estimé / IC utilisé pour dimensionner le papier | 0 / 0,04279 |
| Opérations de la poche nouvelles cotations | 0 |

Le financement est un petit gain ; les commissions ne sont pas la cause principale de la perte.
Le résidu inclut spread, glissement et variations des prix : il ne doit pas être appelé « alpha brut ».
Un instantané ne suffit pas pour reconstituer chaque transaction, sa durée ni une incertitude statistique.
Le calcul suppose le capital initial configuré inchangé depuis la création du compte, sans flux externe.
La courbe persistante peut couvrir plusieurs champions : le PnL cumulé appartient au compte et ne peut
pas être attribué intégralement au seul modèle actuellement installé.

## Pourquoi le backtest positif ne suffit pas

Le rapport du même modèle (`config_hash=1b8c803a29a2`) donne Sharpe 1,36 et +16,4 %/an sur
2023-08 → 2026-08, mais **−7,38 % en 2026**. Deux critères échouent : DSR 0,639 contre 0,95,
PBO 0,353 contre 0,30. Le passage à 7/9 consigné dans le dépôt explique que le modèle ait ensuite été
promu ; l'audit conserve cette règle et les verdicts datés.

Sur les 90 derniers jours de cet historique, l'exposition brute moyenne est seulement **0,000052×** :
le livre adaptatif s'est presque éteint. Le papier force au contraire une taille nominale pour observer
le signal, choix consigné le 24 septembre. Un Sharpe sur les minuscules rendements d'un livre presque
éteint ne justifie pas de lui remettre une exposition nominale. Les périodes déjà examinées ne peuvent
plus servir de nouveau test indépendant en ajustant les paramètres après observation.

Les données brutes nécessaires pour réentraîner et rejouer toute la recherche ne sont pas versionnées.
L'artefact original et des archives publiques ont été récupérés pour le diagnostic limité ci-dessous.
Les chiffres du rapport historique complet ne sont **pas** présentés comme des backtests intégralement
réexécutés avec les corrections. Le financement historique reste celui de Binance alors que l'exécution
privée utilise OKX : cette différence exige encore une mesure sur la bonne place.

## Rejeu réel à prédictions figées

Les deux moteurs ont été exécutés sur les mêmes archives publiques, les mêmes prédictions OOF et les
neuf sous-livres d'origine, sans réentraînement ni réglage après observation. Le compte simulé commence
sans position avec 10 000 USDT le 1er janvier 2026, après 184 jours d'initialisation. Il s'arrête à la clôture
de la bougie du 31 août à 20:00 UTC. Ce protocole ne recrée pas le compte continu du rapport original.

| Mesure | Déployé `22facd4` | Corrigé `b6b52d3` |
|---|---:|---:|
| Rendement net simulé | −6,3076 % | −6,3024 % |
| Équité finale | 9 369,24 USDT | 9 369,76 USDT |
| Drawdown maximal | −6,9783 % | −7,0367 % |
| Sharpe quotidien | −2,0033 | −2,0023 |

**L'amélioration terminale n'est que de 0,52 USDT ; les deux moteurs restent perdants et le drawdown
s'aggrave légèrement.** L'architecture corrigée ne crée pas, dans ce test, un avantage économique.
Sur 11 656 décisions par moteur, aucun plafond final contrôlé n'est dépassé, y compris avec l'ancienne
version ; le nouveau limiteur final n'a jamais dû réduire l'exposition. Les différences viennent ici de la
construction interne des sous-livres, pas d'une activation de ce dernier garde-fou. Les cas limites corrigés
restent vérifiés séparément par les tests de régression.

Les entrées, labels, signaux et IC sont identiques entre les deux exécutions, contrôlés par empreintes.
Les métriques utilisent les mêmes formules ; les auxiliaires et labels ont été vérifiés contre la bibliothèque
complète. Ces dates ayant déjà été étudiées et l'ancien OOF n'ayant pas de provenance d'entraînement
renforcée, ce diagnostic n'est pas un nouveau test indépendant. Il ne teste ni les fills réels d'OKX ni les
réparations du broker et du traitement temps réel.

Protocole, commandes, empreintes et résultats :
[`reports/audit-2026-09-30/replay/`](../reports/audit-2026-09-30/replay/README.md).

## Architecture corrigée

- **Exécution réconciliée** : une réponse absente ou une annulation simplement acceptée ne prouve plus
  qu'un ordre est terminal. Les identifiants incertains sont conservés avant envoi dans un journal durable,
  relus après redémarrage ; les augmentations attendent la réconciliation. La part maker utilise le
  notionnel USDT, y compris quand la quantité est exprimée en contrats OKX.
- **Qualité des observations** : le flux conserve la distinction entre prix réellement observé et prix
  rempli artificiellement. Les entrées sur un contrat sans cotation récente sont refusées.
- **Comptabilité papier** : les bougies manquées sont parcourues pour les stops ; les taux de funding
  publiés en retard sont rapprochés avec les expositions enregistrées, avec déduplication persistée.
  Le modèle reste une approximation par bougies, pas une reconstitution des fills réels d'OKX.
- **Contraintes sur le portefeuille final** : les petits ordres conservés et le plafonnement du nombre
  de positions sont pris en compte avant la validation finale des plafonds et de l'expected shortfall.
- **Validation reproductible** : un cache d'entraînement exige la même empreinte des données, symboles,
  matrices, sources et dépendances. Une ancienne marque fondée seulement sur les dimensions est refusée.
  Rapport, registre des essais et modèle portent la même provenance. Le drawdown inclut le capital initial.
  Les données synthétiques ne peuvent plus donner lieu à une promotion, même si elles franchissent 9/9 critères.
- **Artefact de modèle explicite** : le format 2 vérifie la configuration et les fichiers déclarés ; des
  boosters résiduels non déclarés ne sont plus chargés. Un ancien bundle reste lisible avec un avertissement
  explicite : on ne peut pas prouver rétroactivement l'intégrité de sa configuration non signée.
  Une empreinte n'est pas une signature ; avant migration d'un ancien bundle, comparer sa configuration au
  rapport évalué ou refaire l'évaluation. Le réenregistrer seul ne reconstitue pas cette preuve.
- **Bilan économique indépendant** : `hermes research audit` relit les rendements et l'instantané, publie
  leurs SHA-256, les fenêtres historiques et le PnL papier. Il ne passe aucun ordre et ne promeut aucun modèle.

## Reproduire le bilan

```bash
hermes research audit \
  reports/2026-09-24-research_30m_xl_lb_sres_books-35950527756 \
  --status reports/audit-2026-09-30/paper_status.json
```

Les entrées et le résultat calculé figurent dans `reports/audit-2026-09-30/`. L'instantané versionné est un
extrait économique : ni clés, ni identifiants d'accès, ni détails des positions. Une sortie `--out` existante
est refusée pour ne pas écraser les sources ou une preuve précédente.

## Validation prospective encore nécessaire

Le profil `configs/paper_adaptive.yaml` permet un compte papier séparé avec la taille adaptative de la
recherche et le même modèle ; le compte nominal existant est conservé. Comparer sur les **mêmes dates**
les PnL nets, expositions, turnover et IC réalisés permet de mesurer le coût de la taille nominale.
Le nouveau compte doit commencer avec son propre état ; recycler une courbe ancienne fausserait la comparaison.

Avant de conclure à une amélioration économique : recueillir une trajectoire prospective, vérifier les coûts
et funding sur OKX, puis valider le connecteur en démo. Des tests logiciels verts établissent la correction
des comportements testés ; ils n'établissent pas la rentabilité. Aucun changement de levier, activation LIVE,
fusion ni déploiement du présent lot n'a été effectué pendant l'audit.

## Limites techniques restantes

- Le rapprochement du funding papier porte sur sept jours et les assiettes enregistrées depuis la mise
  à jour ; il ne reconstruit pas les positions antérieures inconnues.
- Un stop dans une bougie reçue normalement pendant une interruption est rejoué. Une bougie individuellement
  absente, reçue seulement après l'avancement du curseur global, ne rejoue pas encore son ancien stop.
- Le compte JSON et le journal SQLite ne forment pas une transaction unique : la déduplication du funding
  traite ses reprises, mais une panne peut encore priver le journal d'un fill de stop déjà comptabilisé.
- La comptabilité analytique de la poche nouvelles cotations conserve son ancien curseur de financement ;
  le diagnostic n'en observe aucune opération. Sa migration reste à faire avant d'utiliser son PnL comme preuve.
- Le remplacement à chaud par une stratégie réellement différente conserve encore la mémoire des scores/IC.
  Une comparaison de stratégies doit utiliser des états isolés ; une séparation automatique par version reste à faire.

## Vérifications exécutées

Le 30 septembre 2026, la suite complète donne **248 tests réussis, 0 échec, 0 erreur, 0 ignoré** en 215 s
(`OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1 python -m pytest -q`). Ruff lint,
Ruff format et `git diff --check` passent. Deux avertissements attendus concernent l'import des anciens
bundles sans empreinte de configuration. Le bilan JSON est identique à son recalcul par la nouvelle commande.
Résultat machine : `reports/audit-2026-09-30/validation.json`.

La [CI GitHub du code corrigé](https://github.com/speed25200-cyber/Hermes-Fork/actions/runs/36704003062)
est passée. Le rejeu différentiel sur données réelles décrit ci-dessus est terminé ; il ne démontre pas de
rentabilité. Aucun test d'ordres connecté n'a été exécuté et aucune nouvelle décision de promotion n'a été
prise. Les tests de régression utilisent des scénarios contrôlés.
