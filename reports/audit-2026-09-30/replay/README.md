# Rejeu différentiel figé — janvier à août 2026

**Les deux moteurs perdent de l'argent.** Pour 10 000 USDT, le moteur corrigé termine à 9 369,76 USDT,
contre 9 369,24 pour la version déployée : seulement +0,52 USDT d'écart. Son drawdown maximal est un peu
plus mauvais. Ce résultat ne justifie ni une promotion ni une augmentation de risque.

| Mesure | Déployé `22facd4` | Corrigé `b6b52d3` |
|---|---:|---:|
| Rendement net simulé | −6,307599 % | −6,302415 % |
| Équité finale, USDT | 9 369,2401 | 9 369,7585 |
| Drawdown maximal, capital initial inclus | −6,978327 % | −7,036716 % |
| Sharpe sur rendements quotidiens | −2,0033 | −2,0023 |
| Décisions contrôlées | 11 656 | 11 656 |
| Dépassements des contraintes finales contrôlées | 0 | 0 |

Le dernier limiteur du moteur corrigé n'a jamais dû réduire le portefeuille (`min_final_scale=1`).
Cet échantillon ne reproduit donc pas les cas limites couverts par les tests de régression. Les différences
de trajectoire peuvent provenir des corrections de construction des sous-livres. Le compte papier du VPS
et le compte continu du rapport original sont des trajectoires distinctes de cette simulation.

## Protocole

- Déclaration datée avant le rejeu, aucune recherche de paramètres ni réentraînement.
- Configuration et neuf sous-livres du modèle conservés ; aucun signal directionnel ajouté.
- Prédictions OOF originales, masque historique défini par leurs cellules renseignées ; mêmes entrées
  et mêmes signaux contrôlés par empreintes avant de lancer chaque moteur.
- Archives publiques Binance 15 minutes, agrégées en 30 minutes. 184 jours d'initialisation depuis le
  1er juillet 2025 ; compte sans position au 1er janvier 2026. Dernière bougie : 31 août, 19:30–20:00 UTC.
- Auxiliaires de risque et labels calculés avec les fonctions du dépôt ; parité exacte avec la bibliothèque
  complète vérifiée sur 30 jours. Aucun nouveau modèle ajusté.
- Frais, spread, impact et funding simulés selon la configuration originale. Funding Binance et coûts
  modélisés : ce ne sont pas les fills et coûts observés sur OKX.
- Même calcul des métriques pour les deux versions, drawdown depuis le capital initial inclus.
- Observateur non mutant des décisions finales, testé sur les deux versions. Les poids, ADV et ES sont
  contrôlés avec l'équité de décision avant frais, et la covariance utilisée à cette décision. La neutralité
  porte sur l'exposition bêta (plafond 0,05), pas sur le net en dollars.

Les 20 488 bougies d'entrée contiennent toujours 30 membres ; 112 contrats différents interviennent.
Aucun OHLCV invalide ni VWAP manquant n'est détecté sur les membres. Les 542 couples contrat-mois actifs
contiennent au moins une observation de funding ; cela ne prouve pas l'exhaustivité de tous les règlements.

La mémoire EWMA de l'IC conserve une petite contribution au-delà des 184 jours chargés : ce démarrage
commun reste une approximation. Les dates ont déjà été examinées ; ce n'est pas un nouveau test intact.
L'ancien artefact OOF ne porte pas la provenance renforcée introduite par cette PR. Son usage explicite
dans ce diagnostic ne le rend pas admissible au nouveau cache d'entraînement.

## Fichiers et reproduction

`comparison.json` contient le bilan et les empreintes des fichiers ; `inputs.json` est commun aux deux
exécutions. `baseline.json` et `corrected.json` contiennent les métriques et contrôles détaillés. Les CSV
quotidiens et mensuels permettent de recomposer les rendements. Les sommes de frais/PnL dans les CSV
sont des contributions rapportées aux équités des bougies : elles ne s'additionnent pas directement
en dollars ni en rendement composé.

L'artefact provient du [run Hermes 35950527756](https://github.com/speed25200-cyber/Hermes/actions/runs/35950527756),
nom `research-research_30m_xl_lb_sres_books-35950527756`, ID `10792490862`. L'archive ZIP récupérée a pour SHA-256
`adb69d3e7225e08f03cf853f51d3299daf0f3e4031966a537ef097cec1ac123c`. Cet artefact GitHub a une rétention limitée ;
le panneau brut et les gros Parquet ne sont pas ajoutés au dépôt. Les empreintes enregistrées permettent
de constater un éventuel changement des entrées, pas de reconstruire des données devenues indisponibles.

Depuis la racine du dépôt corrigé, avec l'environnement installé et `gh` connecté :

```bash
gh run download 35950527756 --repo speed25200-cyber/Hermes \
  --name research-research_30m_xl_lb_sres_books-35950527756 \
  --dir artifacts/research_books
mkdir -p artifacts/diagnostic_2026
cp reports/audit-2026-09-30/replay/declaration.json artifacts/diagnostic_2026/declaration.json
HERMES_ARCHIVE_MIRROR=cdn .venv/bin/python reports/audit-2026-09-30/replay/fetch_panel.py
git worktree add --detach ../Hermes-Fork-baseline 22facd44104a7994cc3365f0696898da4c98d789
```

Le ZIP doit produire `artifacts/research_books/reports/2026-09-24-research_30m_xl_lb_sres_books-35950527756/`.
Exécuter ensuite le même script avec les deux moteurs, dans des répertoires de sortie distincts :

```bash
export OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1
PYTHONPATH=../Hermes-Fork-baseline/src .venv/bin/python reports/audit-2026-09-30/replay/replay.py \
  --panel artifacts/diagnostic_2026/panel \
  --artifact artifacts/research_books/reports/2026-09-24-research_30m_xl_lb_sres_books-35950527756 \
  --declaration reports/audit-2026-09-30/replay/declaration.json \
  --output artifacts/diagnostic_2026/reproduced-baseline
PYTHONPATH=src .venv/bin/python reports/audit-2026-09-30/replay/replay.py \
  --panel artifacts/diagnostic_2026/panel \
  --artifact artifacts/research_books/reports/2026-09-24-research_30m_xl_lb_sres_books-35950527756 \
  --declaration reports/audit-2026-09-30/replay/declaration.json \
  --output artifacts/diagnostic_2026/reproduced-corrected \
  --reference-inputs artifacts/diagnostic_2026/reproduced-baseline/inputs.json
```

Le moteur corrigé exécuté pour ce bilan est `b6b52d32109147535204c5d2cda9a76ccec14bcd` ; les commits de
documentation qui l'accompagnent ne changent pas son code. Pour une reproduction historique après d'autres
modifications, utiliser un second worktree à ce commit et pointer `PYTHONPATH` vers son dossier `src`.

Deux problèmes du script de diagnostic ont été corrigés avant la comparaison finale : normalisation des
unités d'horodatage et exclusion des appels internes du limiteur dans l'observateur final. Aucun paramètre
de stratégie n'a changé. La série de rendements du premier moteur est strictement identique après la
réparation de l'observateur. Les corrections du broker et du moteur temps réel ne sont pas exercées par
ce rejeu historique.
