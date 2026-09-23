# Rapport de recherche Hermes

*Généré le 2026-09-23T02:16:19+00:00 · configuration `27b5fd3568ff` · 4293 s de calcul.*

## Verdict : ⛔ NON PROMU — interdit de capital réel

Toutes les mesures ci-dessous sont **hors échantillon** (walk-forward : chaque prédiction vient d'un modèle entraîné uniquement sur le passé) et **nettes de frais, spread, impact et funding**.

| Porte | Valeur | Seuil | |
|---|---:|---:|:-:|
| Sharpe dégonflé (DSR) — probabilité que le vrai Sharpe > le meilleur hasard parmi les essais | 0.000 | ≥ 0.95 | ❌ |
| p-valeur exacte face au nul (mêmes scores permutés entre contrats par blocs d'une semaine) | 1.000 | ≤ 0.05 | ❌ |
| Probabilité de sur-ajustement du backtest (PBO, CSCV sur la grille) | 0.127 | ≤ 0.30 | ✅ |
| Sharpe annualisé net de coûts (quotidien) | -2.138 | ≥ 0.80 | ❌ |
| Part des années civiles positives (années de moins de 90 jours exclues) | 0.250 | ≥ 0.60 | ❌ |
| Mois hors échantillon | 37.105 | ≥ 12.00 | ✅ |
| Sharpe avec coûts doublés | -1.247 | ≥ 0.00 | ❌ |
| Sharpe avec une barre de latence en plus | -1.550 | ≥ 0.00 | ❌ |

## Données

- Source : `binance_archive`, barres `30m`, du 2022-06-01 au 2026-08-31.
- 289 contrats ayant figuré dans l'univers point-in-time (≈ 29 membres en moyenne), 2 149 703 échantillons × 114 variables.
- Hors échantillon à partir du 2023-07-31.

## Qualité de prédiction (IC transversal de Spearman, cible résiduelle nette du funding)

| Horizon | IC moyen | t (Newey-West) | IC/σ | % périodes > 0 |
|---|---:|---:|---:|---:|
| 2 barres | 0.0536 | 30.97 | 0.221 | 59.0 % |
| 4 barres | 0.0542 | 25.63 | 0.223 | 59.1 % |
| 8 barres | 0.0537 | 20.22 | 0.222 | 59.0 % |

Par modèle (horizon de détention) : `gbm_h4` IC 0.0554 (t 27.4), `ridge_h4` IC 0.0482 (t 22.8).

IC réalisé par année : 2023 : 0.0254, 2024 : 0.0414, 2025 : 0.0347, 2026 : 0.0341.

Modèle de direction du marché : corrélation 0.0189 (t ≈ 2.2), par année {2023: -0.0002, 2024: 0.0492, 2025: 0.0096, 2026: 0.0041} ; porte propre non franchie. Sharpe du livre avec exposition nette pilotée : -2.07 (utilisé en production : non).

## Performance nette du portefeuille (bêta-neutre, maker d'abord)

| Mesure | Valeur |
|---|---:|
| Sharpe annualisé (quotidien) | -2.14 |
| Intervalle bootstrap 90 % du Sharpe | [-3.64 ; -1.69] |
| Rendement annualisé (CAGR) | -8.8 % |
| Volatilité annualisée | 4.1 % |
| Perte maximale (drawdown) | -25.0 % |
| Calmar | -0.35 |
| Pire / meilleur jour | -1.73 % / 2.06 % |
| Jours positifs | 32.5 % |
| PnL brut annuel (avant coûts) | 5.3 % |
| Frais / spread / impact annuels | 8.8 % / 2.7 % / 2.9 % |
| Funding annuel (+ = encaissé) | -0.2 % |
| Rotation annuelle (× capital) | 273 |
| Exposition brute / nette / bêta moyennes | 0.06 / 0.00 / 0.00 |
| Positions moyennes | 13.0 |

### Par année

| Année | Rendement | Sharpe | Drawdown max |
|---|---:|---:|---:|
| 2023 | -21.5 % | -5.21 | -21.7 % |
| 2024 | -4.1 % | -5.45 | -4.2 % |
| 2025 | -0.0 % | -4.94 | -0.0 % |
| 2026 | 0.0 % | 0.28 | -0.0 % |

## Tests statistiques

- **Probabilistic Sharpe Ratio** (vrai Sharpe > 0) : 0.000.
- **Deflated Sharpe Ratio** (6 essais effectifs comptés) : 0.000.
- **Nul par permutation** : Sharpe réel au percentile 0 %, p-valeur exacte 1.000 ; 95ᵉ percentile du nul 1.01 (24 répliques).
- **Test SPA de Hansen** (p-valeur, H0 : aucun avantage) : 1.000.
- **PBO** sur la grille : 0.127.
- **Historique minimal** pour conclure à 95 % : — jours.
- **Stress** : coûts ×2 → Sharpe -1.25 ; une barre de latence → Sharpe -1.55.

### Grille de construction (base du PBO)

| Configuration | Sharpe | CAGR | Drawdown | Rotation |
|---|---:|---:|---:|---:|
| `{'holding_horizon': 2, 'cost_aversion': 0.5}` | -1.44 | -8.9 % | -25.0 % | 295 |
| `{'holding_horizon': 2, 'cost_aversion': 1.0}` | -1.94 | -8.8 % | -25.0 % | 229 |
| `{'holding_horizon': 2, 'cost_aversion': 2.0}` | -1.86 | -7.9 % | -25.0 % | 227 |
| `{'holding_horizon': 4, 'cost_aversion': 0.5}` | -1.25 | -8.9 % | -25.0 % | 259 |
| `{'holding_horizon': 4, 'cost_aversion': 1.0}` | -2.14 | -8.8 % | -25.0 % | 273 |
| `{'holding_horizon': 4, 'cost_aversion': 2.0}` | -0.42 | -4.5 % | -25.0 % | 447 |
| `{'holding_horizon': 8, 'cost_aversion': 0.5}` | -1.44 | -8.9 % | -25.0 % | 267 |
| `{'holding_horizon': 8, 'cost_aversion': 1.0}` | -2.37 | -8.9 % | -25.0 % | 302 |
| `{'holding_horizon': 8, 'cost_aversion': 2.0}` | -0.82 | -5.5 % | -25.0 % | 353 |

## Variables les plus utilisées (gain LightGBM, moyenne des plis)

`cs_premium` 128.5 %, `iret_60m` 101.3 %, `iret_30m` 69.8 %, `cs_ret_60m` 53.9 %, `hour_cos` 51.4 %, `cs_vol_level` 50.5 %, `premium_z` 47.9 %, `iret_120m` 42.7 %, `iret_240m` 40.0 %, `log_dollar_volume` 38.8 %, `trade_size` 35.3 %, `cs_ret_240m` 32.7 %, `amihud_long` 30.0 %, `iret_480m` 28.8 %, `gk_vol_day` 27.2 %

## Lecture honnête

Un backtest, même hors échantillon, reste une estimation : l'intervalle de confiance du Sharpe ci-dessus dit à quel point. La porte de promotion est volontairement sévère ; un résultat qui ne la franchit pas ne trade pas en réel, quel que soit l'attrait des chiffres. Un résultat qui la franchit démarre en papier, puis en réel à capital réduit, et reste surveillé par l'IC réalisé en continu.
