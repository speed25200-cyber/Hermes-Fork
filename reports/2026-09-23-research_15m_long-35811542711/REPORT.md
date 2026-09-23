# Rapport de recherche Hermes

*Généré le 2026-09-23T04:35:25+00:00 · configuration `d5d3e407c6ed` · 6629 s de calcul.*

## Verdict : ⛔ NON PROMU — interdit de capital réel

Toutes les mesures ci-dessous sont **hors échantillon** (walk-forward : chaque prédiction vient d'un modèle entraîné uniquement sur le passé) et **nettes de frais, spread, impact et funding**.

| Porte | Valeur | Seuil | |
|---|---:|---:|:-:|
| Sharpe dégonflé (DSR) — probabilité que le vrai Sharpe > le meilleur hasard parmi les essais | 0.584 | ≥ 0.95 | ❌ |
| p-valeur exacte face au nul (mêmes scores permutés entre contrats par blocs d'une semaine) | 0.080 | ≤ 0.05 | ❌ |
| Probabilité de sur-ajustement du backtest (PBO, CSCV sur la grille) | 0.817 | ≤ 0.30 | ❌ |
| Sharpe annualisé net de coûts (quotidien) | 0.705 | ≥ 0.80 | ❌ |
| Part des années civiles positives (années de moins de 90 jours exclues) | 0.500 | ≥ 0.60 | ❌ |
| Mois hors échantillon | 37.105 | ≥ 12.00 | ✅ |
| Sharpe avec coûts doublés | -0.061 | ≥ 0.00 | ❌ |
| Sharpe avec une barre de latence en plus | 0.596 | ≥ 0.00 | ✅ |

## Données

- Source : `binance_archive`, barres `15m`, du 2022-06-01 au 2026-08-31.
- 289 contrats ayant figuré dans l'univers point-in-time (≈ 29 membres en moyenne), 4 299 282 échantillons × 117 variables.
- Hors échantillon à partir du 2023-07-31.

## Qualité de prédiction (IC transversal de Spearman, cible résiduelle nette du funding)

| Horizon | IC moyen | t (Newey-West) | IC/σ | % périodes > 0 |
|---|---:|---:|---:|---:|
| 16 barres | 0.0560 | 16.90 | 0.209 | 58.5 % |
| 32 barres | 0.0625 | 13.98 | 0.232 | 59.2 % |
| 96 barres | 0.0730 | 10.03 | 0.267 | 60.7 % |

Par modèle (horizon de détention) : `gbm_h32` IC 0.0611 (t 14.4), `ridge_h32` IC 0.0541 (t 12.5).

IC réalisé par année : 2023 : 0.0089, 2024 : 0.0416, 2025 : 0.0568, 2026 : 0.0363.

Modèle de direction du marché : corrélation 0.0285 (t ≈ 1.6), par année {2023: -0.0093, 2024: 0.0788, 2025: 0.0528, 2026: -0.0055} ; porte propre non franchie. Sharpe du livre avec exposition nette pilotée : 1.04 (utilisé en production : non).

## Performance nette du portefeuille (bêta-neutre, maker d'abord)

| Mesure | Valeur |
|---|---:|
| Sharpe annualisé (quotidien) | 0.71 |
| Intervalle bootstrap 90 % du Sharpe | [-0.30 ; 1.95] |
| Rendement annualisé (CAGR) | 11.1 % |
| Volatilité annualisée | 13.8 % |
| Perte maximale (drawdown) | -20.2 % |
| Calmar | 0.55 |
| Pire / meilleur jour | -3.69 % / 3.33 % |
| Jours positifs | 53.8 % |
| PnL brut annuel (avant coûts) | 33.3 % |
| Frais / spread / impact annuels | 14.1 % / 4.6 % / 1.8 % |
| Funding annuel (+ = encaissé) | -1.3 % |
| Rotation annuelle (× capital) | 441 |
| Exposition brute / nette / bêta moyennes | 0.63 / 0.11 / 0.01 |
| Positions moyennes | 23.6 |

### Par année

| Année | Rendement | Sharpe | Drawdown max |
|---|---:|---:|---:|
| 2023 | -7.8 % | -2.16 | -10.0 % |
| 2024 | 68.5 % | 3.29 | -7.6 % |
| 2025 | 1.2 % | 0.15 | -19.8 % |
| 2026 | -11.9 % | -1.85 | -19.3 % |

## Tests statistiques

- **Probabilistic Sharpe Ratio** (vrai Sharpe > 0) : 0.919.
- **Deflated Sharpe Ratio** (5 essais effectifs comptés) : 0.584.
- **Nul par permutation** : Sharpe réel au percentile 96 %, p-valeur exacte 0.080 ; 95ᵉ percentile du nul 0.33 (24 répliques).
- **Test SPA de Hansen** (p-valeur, H0 : aucun avantage) : 0.122.
- **PBO** sur la grille : 0.817.
- **Historique minimal** pour conclure à 95 % : 1558 jours.
- **Stress** : coûts ×2 → Sharpe -0.06 ; une barre de latence → Sharpe 0.60.

### Grille de construction (base du PBO)

| Configuration | Sharpe | CAGR | Drawdown | Rotation |
|---|---:|---:|---:|---:|
| `{'holding_horizon': 16, 'cost_aversion': 0.5}` | 0.56 | 7.8 % | -21.4 % | 652 |
| `{'holding_horizon': 16, 'cost_aversion': 1.0}` | 0.65 | 9.3 % | -20.5 % | 458 |
| `{'holding_horizon': 16, 'cost_aversion': 2.0}` | 0.94 | 12.9 % | -19.6 % | 249 |
| `{'holding_horizon': 32, 'cost_aversion': 0.5}` | 0.81 | 13.4 % | -20.8 % | 545 |
| `{'holding_horizon': 32, 'cost_aversion': 1.0}` | 0.71 | 11.1 % | -20.2 % | 441 |
| `{'holding_horizon': 32, 'cost_aversion': 2.0}` | 0.97 | 14.5 % | -20.4 % | 291 |
| `{'holding_horizon': 96, 'cost_aversion': 0.5}` | 0.93 | 15.8 % | -21.4 % | 336 |
| `{'holding_horizon': 96, 'cost_aversion': 1.0}` | 0.94 | 16.0 % | -21.0 % | 300 |
| `{'holding_horizon': 96, 'cost_aversion': 2.0}` | 0.86 | 13.7 % | -20.5 % | 244 |

## Variables les plus utilisées (gain LightGBM, moyenne des plis)

`cs_vol_level` 122.9 %, `amihud_long` 113.2 %, `log_dollar_volume` 72.6 %, `trade_size` 57.0 %, `cs_funding_sum_1440m` 47.5 %, `cs_premium` 46.1 %, `cs_amihud_long` 43.4 %, `ivol_share` 42.2 %, `mkt_funding` 39.6 %, `beta` 38.9 %, `kurt_long` 37.3 %, `volvol_long` 35.9 %, `cs_beta` 35.7 %, `mkt_vol` 34.3 %, `dow_cos` 32.9 %

## Lecture honnête

Un backtest, même hors échantillon, reste une estimation : l'intervalle de confiance du Sharpe ci-dessus dit à quel point. La porte de promotion est volontairement sévère ; un résultat qui ne la franchit pas ne trade pas en réel, quel que soit l'attrait des chiffres. Un résultat qui la franchit démarre en papier, puis en réel à capital réduit, et reste surveillé par l'IC réalisé en continu.
