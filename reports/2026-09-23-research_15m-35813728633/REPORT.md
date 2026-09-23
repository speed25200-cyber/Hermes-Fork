# Rapport de recherche Hermes

*Généré le 2026-09-23T04:53:31+00:00 · configuration `606a7080938a` · 5712 s de calcul.*

## Verdict : ⛔ NON PROMU — interdit de capital réel

Toutes les mesures ci-dessous sont **hors échantillon** (walk-forward : chaque prédiction vient d'un modèle entraîné uniquement sur le passé) et **nettes de frais, spread, impact et funding**.

| Porte | Valeur | Seuil | |
|---|---:|---:|:-:|
| Sharpe dégonflé (DSR) — probabilité que le vrai Sharpe > le meilleur hasard parmi les essais | 0.000 | ≥ 0.95 | ❌ |
| p-valeur exacte face au nul (mêmes scores permutés entre contrats par blocs d'une semaine) | 0.520 | ≤ 0.05 | ❌ |
| Probabilité de sur-ajustement du backtest (PBO, CSCV sur la grille) | 0.127 | ≤ 0.30 | ✅ |
| Sharpe annualisé net de coûts (quotidien) | -0.264 | ≥ 0.80 | ❌ |
| Part des années civiles positives (années de moins de 90 jours exclues) | 0.250 | ≥ 0.60 | ❌ |
| Mois hors échantillon | 37.105 | ≥ 12.00 | ✅ |
| Sharpe avec coûts doublés | -2.257 | ≥ 0.00 | ❌ |
| Sharpe avec une barre de latence en plus | -0.719 | ≥ 0.00 | ❌ |

## Données

- Source : `binance_archive`, barres `15m`, du 2022-06-01 au 2026-08-31.
- 289 contrats ayant figuré dans l'univers point-in-time (≈ 29 membres en moyenne), 4 299 282 échantillons × 117 variables.
- Hors échantillon à partir du 2023-07-31.

## Qualité de prédiction (IC transversal de Spearman, cible résiduelle nette du funding)

| Horizon | IC moyen | t (Newey-West) | IC/σ | % périodes > 0 |
|---|---:|---:|---:|---:|
| 2 barres | 0.0549 | 42.82 | 0.233 | 59.4 % |
| 4 barres | 0.0530 | 34.59 | 0.225 | 59.1 % |
| 8 barres | 0.0514 | 27.84 | 0.218 | 58.9 % |

Par modèle (horizon de détention) : `gbm_h4` IC 0.0534 (t 36.4), `ridge_h4` IC 0.0493 (t 31.4).

IC réalisé par année : 2023 : 0.0152, 2024 : 0.0288, 2025 : 0.0253, 2026 : 0.0230.

Modèle de direction du marché : corrélation 0.0170 (t ≈ 2.8), par année {2023: 0.0157, 2024: 0.0385, 2025: 0.001, 2026: 0.0061} ; porte propre franchie. Sharpe du livre avec exposition nette pilotée : -0.48 (utilisé en production : non).

## Performance nette du portefeuille (bêta-neutre, maker d'abord)

| Mesure | Valeur |
|---|---:|
| Sharpe annualisé (quotidien) | -0.26 |
| Intervalle bootstrap 90 % du Sharpe | [-1.32 ; 0.73] |
| Rendement annualisé (CAGR) | -3.4 % |
| Volatilité annualisée | 10.6 % |
| Perte maximale (drawdown) | -22.4 % |
| Calmar | -0.15 |
| Pire / meilleur jour | -3.48 % / 2.97 % |
| Jours positifs | 52.4 % |
| PnL brut annuel (avant coûts) | 13.1 % |
| Frais / spread / impact annuels | 12.2 % / 3.2 % / 2.4 % |
| Funding annuel (+ = encaissé) | 1.9 % |
| Rotation annuelle (× capital) | 382 |
| Exposition brute / nette / bêta moyennes | 0.44 / 0.04 / 0.01 |
| Positions moyennes | 22.5 |

### Par année

| Année | Rendement | Sharpe | Drawdown max |
|---|---:|---:|---:|
| 2023 | -8.7 % | -2.02 | -10.2 % |
| 2024 | 16.1 % | 1.22 | -9.0 % |
| 2025 | -7.0 % | -0.58 | -15.3 % |
| 2026 | -8.9 % | -3.81 | -9.5 % |

## Tests statistiques

- **Probabilistic Sharpe Ratio** (vrai Sharpe > 0) : 0.308.
- **Deflated Sharpe Ratio** (16 essais effectifs comptés) : 0.000.
- **Nul par permutation** : Sharpe réel au percentile 50 %, p-valeur exacte 0.520 ; 95ᵉ percentile du nul 0.76 (24 répliques).
- **Test SPA de Hansen** (p-valeur, H0 : aucun avantage) : 1.000.
- **PBO** sur la grille : 0.127.
- **Historique minimal** pour conclure à 95 % : — jours.
- **Stress** : coûts ×2 → Sharpe -2.26 ; une barre de latence → Sharpe -0.72.

### Économie du signal sans arrêt (diagnostic, hors porte)

Même stratégie sur toute la période, contrôles de drawdown et de perte journalière désactivés : ce que le signal rapporte et coûte réellement, année par année.

- Sharpe -0.22, CAGR -3.8 %, drawdown max -28.8 % ;
- P&L brut 9.1 %/an contre coûts 15.2 %/an, rotation 319×/an, exposition brute moyenne 0.62 ;
- par année : 2023 : -9.1 % ; 2024 : 16.0 % ; 2025 : 3.8 % ; 2026 : -18.9 %.

### Grille de construction (base du PBO)

| Configuration | Sharpe | CAGR | Drawdown | Rotation |
|---|---:|---:|---:|---:|
| `{'holding_horizon': 2, 'cost_aversion': 0.5}` | -1.70 | -8.9 % | -25.0 % | 237 |
| `{'holding_horizon': 2, 'cost_aversion': 1.0}` | -2.21 | -8.8 % | -24.9 % | 271 |
| `{'holding_horizon': 2, 'cost_aversion': 2.0}` | -0.13 | -1.6 % | -14.0 % | 65 |
| `{'holding_horizon': 4, 'cost_aversion': 0.5}` | -2.26 | -8.9 % | -25.0 % | 250 |
| `{'holding_horizon': 4, 'cost_aversion': 1.0}` | -0.26 | -3.4 % | -22.4 % | 382 |
| `{'holding_horizon': 4, 'cost_aversion': 2.0}` | -0.23 | -2.4 % | -15.2 % | 86 |
| `{'holding_horizon': 8, 'cost_aversion': 0.5}` | -1.42 | -8.8 % | -25.0 % | 379 |
| `{'holding_horizon': 8, 'cost_aversion': 1.0}` | -0.31 | -4.1 % | -23.6 % | 413 |
| `{'holding_horizon': 8, 'cost_aversion': 2.0}` | -1.39 | -6.9 % | -22.9 % | 183 |

## Variables les plus utilisées (gain LightGBM, moyenne des plis)

`cs_premium` 141.0 %, `iret_30m` 116.5 %, `iret_15m` 104.2 %, `iret_60m` 98.9 %, `premium_z` 62.1 %, `trade_size` 40.7 %, `iret_120m` 35.3 %, `hour_cos` 34.6 %, `cs_ret_60m` 32.2 %, `cs_vol_level` 31.3 %, `cs_premium_chg_8h` 30.9 %, `ret_15m` 30.0 %, `cs_ret_120m` 30.0 %, `premium` 29.9 %, `cs_ret_30m` 29.7 %

## Lecture honnête

Un backtest, même hors échantillon, reste une estimation : l'intervalle de confiance du Sharpe ci-dessus dit à quel point. La porte de promotion est volontairement sévère ; un résultat qui ne la franchit pas ne trade pas en réel, quel que soit l'attrait des chiffres. Un résultat qui la franchit démarre en papier, puis en réel à capital réduit, et reste surveillé par l'IC réalisé en continu.
