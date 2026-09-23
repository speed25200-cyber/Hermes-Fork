# Rapport de recherche Hermes

*Généré le 2026-09-23T02:43:50+00:00 · configuration `91db32cb1ca7` · 5944 s de calcul.*

## Verdict : ⛔ NON PROMU — interdit de capital réel

Toutes les mesures ci-dessous sont **hors échantillon** (walk-forward : chaque prédiction vient d'un modèle entraîné uniquement sur le passé) et **nettes de frais, spread, impact et funding**.

| Porte | Valeur | Seuil | |
|---|---:|---:|:-:|
| Sharpe dégonflé (DSR) — probabilité que le vrai Sharpe > le meilleur hasard parmi les essais | 0.000 | ≥ 0.95 | ❌ |
| p-valeur exacte face au nul (mêmes scores permutés entre contrats par blocs d'une semaine) | 1.000 | ≤ 0.05 | ❌ |
| Probabilité de sur-ajustement du backtest (PBO, CSCV sur la grille) | 0.349 | ≤ 0.30 | ❌ |
| Sharpe annualisé net de coûts (quotidien) | -1.748 | ≥ 0.80 | ❌ |
| Part des années civiles positives (années de moins de 90 jours exclues) | 0.000 | ≥ 0.60 | ❌ |
| Mois hors échantillon | 37.105 | ≥ 12.00 | ✅ |
| Sharpe avec coûts doublés | -1.118 | ≥ 0.00 | ❌ |
| Sharpe avec une barre de latence en plus | -2.624 | ≥ 0.00 | ❌ |

## Données

- Source : `binance_archive`, barres `15m`, du 2022-06-01 au 2026-08-31.
- 289 contrats ayant figuré dans l'univers point-in-time (≈ 29 membres en moyenne), 4 299 402 échantillons × 117 variables.
- Hors échantillon à partir du 2023-07-31.

## Qualité de prédiction (IC transversal de Spearman, cible résiduelle nette du funding)

| Horizon | IC moyen | t (Newey-West) | IC/σ | % périodes > 0 |
|---|---:|---:|---:|---:|
| 2 barres | 0.0550 | 43.29 | 0.233 | 59.4 % |
| 4 barres | 0.0532 | 34.85 | 0.226 | 59.2 % |
| 8 barres | 0.0516 | 28.11 | 0.220 | 58.9 % |

Par modèle (horizon de détention) : `gbm_h4` IC 0.0538 (t 37.1), `ridge_h4` IC 0.0493 (t 31.4).

IC réalisé par année : 2023 : 0.0333, 2024 : 0.0403, 2025 : 0.0335, 2026 : 0.0344.

Modèle de direction du marché : corrélation 0.0172 (t ≈ 2.8), par année {2023: 0.0145, 2024: 0.0389, 2025: 0.0013, 2026: 0.0061} ; porte propre franchie. Sharpe du livre avec exposition nette pilotée : -1.74 (utilisé en production : oui).

## Performance nette du portefeuille (bêta-neutre, maker d'abord)

| Mesure | Valeur |
|---|---:|
| Sharpe annualisé (quotidien) | -1.75 |
| Intervalle bootstrap 90 % du Sharpe | [-4.05 ; -2.23] |
| Rendement annualisé (CAGR) | -8.9 % |
| Volatilité annualisée | 2.8 % |
| Perte maximale (drawdown) | -25.0 % |
| Calmar | -0.36 |
| Pire / meilleur jour | -1.93 % / 1.06 % |
| Jours positifs | 34.0 % |
| PnL brut annuel (avant coûts) | 3.5 % |
| Frais / spread / impact annuels | 7.8 % / 2.1 % / 2.5 % |
| Funding annuel (+ = encaissé) | -0.4 % |
| Rotation annuelle (× capital) | 242 |
| Exposition brute / nette / bêta moyennes | 0.03 / 0.00 / 0.00 |
| Positions moyennes | 8.4 |

### Par année

| Année | Rendement | Sharpe | Drawdown max |
|---|---:|---:|---:|
| 2023 | -24.9 % | -8.85 | -24.9 % |
| 2024 | -0.2 % | -7.21 | -0.2 % |
| 2025 | -0.0 % | -3.21 | -0.0 % |
| 2026 | -0.0 % | -1.52 | -0.0 % |

## Tests statistiques

- **Probabilistic Sharpe Ratio** (vrai Sharpe > 0) : 0.000.
- **Deflated Sharpe Ratio** (5 essais effectifs comptés) : 0.000.
- **Nul par permutation** : Sharpe réel au percentile 0 %, p-valeur exacte 1.000 ; 95ᵉ percentile du nul 1.02 (24 répliques).
- **Test SPA de Hansen** (p-valeur, H0 : aucun avantage) : 1.000.
- **PBO** sur la grille : 0.349.
- **Historique minimal** pour conclure à 95 % : — jours.
- **Stress** : coûts ×2 → Sharpe -1.12 ; une barre de latence → Sharpe -2.62.

### Grille de construction (base du PBO)

| Configuration | Sharpe | CAGR | Drawdown | Rotation |
|---|---:|---:|---:|---:|
| `{'holding_horizon': 2, 'cost_aversion': 0.5}` | -1.01 | -8.9 % | -25.0 % | 253 |
| `{'holding_horizon': 2, 'cost_aversion': 1.0}` | -1.73 | -8.9 % | -25.0 % | 243 |
| `{'holding_horizon': 2, 'cost_aversion': 2.0}` | -1.60 | -7.7 % | -25.0 % | 257 |
| `{'holding_horizon': 4, 'cost_aversion': 0.5}` | -1.00 | -8.9 % | -25.0 % | 252 |
| `{'holding_horizon': 4, 'cost_aversion': 1.0}` | -1.75 | -8.9 % | -25.0 % | 242 |
| `{'holding_horizon': 4, 'cost_aversion': 2.0}` | -1.98 | -8.6 % | -25.0 % | 286 |
| `{'holding_horizon': 8, 'cost_aversion': 0.5}` | -0.86 | -8.9 % | -24.9 % | 258 |
| `{'holding_horizon': 8, 'cost_aversion': 1.0}` | -1.33 | -8.9 % | -25.0 % | 264 |
| `{'holding_horizon': 8, 'cost_aversion': 2.0}` | -2.54 | -8.9 % | -25.0 % | 280 |

## Variables les plus utilisées (gain LightGBM, moyenne des plis)

`cs_premium` 137.4 %, `iret_30m` 112.8 %, `iret_15m` 102.3 %, `iret_60m` 97.0 %, `premium_z` 60.3 %, `trade_size` 38.9 %, `iret_120m` 34.5 %, `hour_cos` 34.1 %, `cs_ret_60m` 31.4 %, `cs_vol_level` 30.4 %, `cs_premium_chg_8h` 30.1 %, `premium` 29.8 %, `ret_15m` 29.8 %, `cs_ret_120m` 29.7 %, `cs_ret_30m` 29.0 %

## Lecture honnête

Un backtest, même hors échantillon, reste une estimation : l'intervalle de confiance du Sharpe ci-dessus dit à quel point. La porte de promotion est volontairement sévère ; un résultat qui ne la franchit pas ne trade pas en réel, quel que soit l'attrait des chiffres. Un résultat qui la franchit démarre en papier, puis en réel à capital réduit, et reste surveillé par l'IC réalisé en continu.
