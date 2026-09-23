# Rapport de recherche Hermes

*Généré le 2026-09-23T04:01:37+00:00 · configuration `f253dfaa53c6` · 4602 s de calcul.*

## Verdict : ⛔ NON PROMU — interdit de capital réel

Toutes les mesures ci-dessous sont **hors échantillon** (walk-forward : chaque prédiction vient d'un modèle entraîné uniquement sur le passé) et **nettes de frais, spread, impact et funding**.

| Porte | Valeur | Seuil | |
|---|---:|---:|:-:|
| Sharpe dégonflé (DSR) — probabilité que le vrai Sharpe > le meilleur hasard parmi les essais | 0.655 | ≥ 0.95 | ❌ |
| p-valeur exacte face au nul (mêmes scores permutés entre contrats par blocs d'une semaine) | 0.040 | ≤ 0.05 | ✅ |
| Probabilité de sur-ajustement du backtest (PBO, CSCV sur la grille) | 0.702 | ≤ 0.30 | ❌ |
| Sharpe annualisé net de coûts (quotidien) | 0.799 | ≥ 0.80 | ❌ |
| Part des années civiles positives (années de moins de 90 jours exclues) | 0.250 | ≥ 0.60 | ❌ |
| Mois hors échantillon | 37.105 | ≥ 12.00 | ✅ |
| Sharpe avec coûts doublés | 0.099 | ≥ 0.00 | ✅ |
| Sharpe avec une barre de latence en plus | 0.608 | ≥ 0.00 | ✅ |

## Données

- Source : `binance_archive`, barres `30m`, du 2022-06-01 au 2026-08-31.
- 289 contrats ayant figuré dans l'univers point-in-time (≈ 29 membres en moyenne), 2 149 643 échantillons × 114 variables.
- Hors échantillon à partir du 2023-07-31.

## Qualité de prédiction (IC transversal de Spearman, cible résiduelle nette du funding)

| Horizon | IC moyen | t (Newey-West) | IC/σ | % périodes > 0 |
|---|---:|---:|---:|---:|
| 8 barres | 0.0565 | 16.99 | 0.214 | 58.8 % |
| 16 barres | 0.0629 | 13.99 | 0.236 | 59.6 % |
| 48 barres | 0.0734 | 10.10 | 0.273 | 60.9 % |

Par modèle (horizon de détention) : `gbm_h16` IC 0.0615 (t 14.6), `ridge_h16` IC 0.0528 (t 12.1).

IC réalisé par année : 2023 : 0.0137, 2024 : 0.0429, 2025 : 0.0556, 2026 : 0.0367.

Modèle de direction du marché : corrélation 0.0267 (t ≈ 1.6), par année {2023: -0.0151, 2024: 0.0824, 2025: 0.0419, 2026: -0.0079} ; porte propre non franchie. Sharpe du livre avec exposition nette pilotée : 1.07 (utilisé en production : non).

## Performance nette du portefeuille (bêta-neutre, maker d'abord)

| Mesure | Valeur |
|---|---:|
| Sharpe annualisé (quotidien) | 0.80 |
| Intervalle bootstrap 90 % du Sharpe | [-0.23 ; 2.06] |
| Rendement annualisé (CAGR) | 13.2 % |
| Volatilité annualisée | 14.3 % |
| Perte maximale (drawdown) | -19.8 % |
| Calmar | 0.67 |
| Pire / meilleur jour | -3.73 % / 3.26 % |
| Jours positifs | 53.6 % |
| PnL brut annuel (avant coûts) | 35.7 % |
| Frais / spread / impact annuels | 13.4 % / 6.0 % / 2.2 % |
| Funding annuel (+ = encaissé) | -0.7 % |
| Rotation annuelle (× capital) | 418 |
| Exposition brute / nette / bêta moyennes | 0.66 / 0.12 / 0.01 |
| Positions moyennes | 24.2 |

### Par année

| Année | Rendement | Sharpe | Drawdown max |
|---|---:|---:|---:|
| 2023 | -6.4 % | -1.57 | -7.7 % |
| 2024 | 76.5 % | 3.46 | -7.7 % |
| 2025 | -0.5 % | 0.04 | -19.8 % |
| 2026 | -10.6 % | -1.43 | -19.5 % |

## Tests statistiques

- **Probabilistic Sharpe Ratio** (vrai Sharpe > 0) : 0.943.
- **Deflated Sharpe Ratio** (5 essais effectifs comptés) : 0.655.
- **Nul par permutation** : Sharpe réel au percentile 100 %, p-valeur exacte 0.040 ; 95ᵉ percentile du nul 0.25 (24 répliques).
- **Test SPA de Hansen** (p-valeur, H0 : aucun avantage) : 0.098.
- **PBO** sur la grille : 0.702.
- **Historique minimal** pour conclure à 95 % : 1217 jours.
- **Stress** : coûts ×2 → Sharpe 0.10 ; une barre de latence → Sharpe 0.61.

### Grille de construction (base du PBO)

| Configuration | Sharpe | CAGR | Drawdown | Rotation |
|---|---:|---:|---:|---:|
| `{'holding_horizon': 8, 'cost_aversion': 0.5}` | 0.52 | 7.2 % | -21.4 % | 611 |
| `{'holding_horizon': 8, 'cost_aversion': 1.0}` | 0.72 | 10.4 % | -20.5 % | 424 |
| `{'holding_horizon': 8, 'cost_aversion': 2.0}` | 0.79 | 10.9 % | -19.0 % | 231 |
| `{'holding_horizon': 16, 'cost_aversion': 0.5}` | 0.80 | 13.2 % | -20.4 % | 522 |
| `{'holding_horizon': 16, 'cost_aversion': 1.0}` | 0.80 | 13.2 % | -19.8 % | 418 |
| `{'holding_horizon': 16, 'cost_aversion': 2.0}` | 1.01 | 15.6 % | -19.5 % | 275 |
| `{'holding_horizon': 48, 'cost_aversion': 0.5}` | 0.92 | 15.9 % | -20.7 % | 309 |
| `{'holding_horizon': 48, 'cost_aversion': 1.0}` | 0.87 | 14.5 % | -20.7 % | 268 |
| `{'holding_horizon': 48, 'cost_aversion': 2.0}` | 0.97 | 15.5 % | -21.2 % | 211 |

## Variables les plus utilisées (gain LightGBM, moyenne des plis)

`cs_vol_level` 116.7 %, `amihud_long` 109.4 %, `log_dollar_volume` 76.4 %, `cs_amihud_long` 66.2 %, `trade_size` 53.1 %, `cs_funding_sum_1440m` 43.6 %, `cs_premium` 43.2 %, `volvol_long` 42.6 %, `cs_beta` 40.0 %, `dow_cos` 39.7 %, `mkt_funding` 39.5 %, `ivol_share` 39.3 %, `beta` 38.3 %, `kurt_long` 38.2 %, `cs_funding_sum_480m` 33.9 %

## Lecture honnête

Un backtest, même hors échantillon, reste une estimation : l'intervalle de confiance du Sharpe ci-dessus dit à quel point. La porte de promotion est volontairement sévère ; un résultat qui ne la franchit pas ne trade pas en réel, quel que soit l'attrait des chiffres. Un résultat qui la franchit démarre en papier, puis en réel à capital réduit, et reste surveillé par l'IC réalisé en continu.
