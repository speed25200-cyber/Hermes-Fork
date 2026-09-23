# Rapport de recherche Hermes

*Généré le 2026-09-23T06:01:26+00:00 · configuration `8188ec93fcc3` · 6311 s de calcul.*

## Verdict : ⛔ NON PROMU — interdit de capital réel

Toutes les mesures ci-dessous sont **hors échantillon** (walk-forward : chaque prédiction vient d'un modèle entraîné uniquement sur le passé) et **nettes de frais, spread, impact et funding**.

| Porte | Valeur | Seuil | |
|---|---:|---:|:-:|
| Sharpe dégonflé (DSR) — probabilité que le vrai Sharpe > le meilleur hasard parmi les essais | 0.534 | ≥ 0.95 | ❌ |
| p-valeur exacte face au nul (mêmes scores permutés entre contrats par blocs d'une semaine) | 0.040 | ≤ 0.05 | ✅ |
| Probabilité de sur-ajustement du backtest (PBO, CSCV sur la grille) | 0.595 | ≤ 0.30 | ❌ |
| Sharpe annualisé net de coûts (quotidien) | 0.740 | ≥ 0.80 | ❌ |
| Part des années civiles positives (années de moins de 90 jours exclues) | 0.500 | ≥ 0.60 | ❌ |
| Mois hors échantillon | 37.105 | ≥ 12.00 | ✅ |
| Sharpe avec coûts doublés | 0.325 | ≥ 0.00 | ✅ |
| Sharpe avec une barre de latence en plus | 0.649 | ≥ 0.00 | ✅ |

## Données

- Source : `binance_archive`, barres `15m`, du 2022-06-01 au 2026-08-31.
- 289 contrats ayant figuré dans l'univers point-in-time (≈ 29 membres en moyenne), 4 299 282 échantillons × 117 variables.
- Hors échantillon à partir du 2023-07-31.

## Qualité de prédiction (IC transversal de Spearman, cible résiduelle nette du funding)

| Horizon | IC moyen | t (Newey-West) | IC/σ | % périodes > 0 |
|---|---:|---:|---:|---:|
| 32 barres | 0.0588 | 12.23 | 0.210 | 58.5 % |
| 96 barres | 0.0740 | 9.36 | 0.261 | 61.0 % |
| 192 barres | 0.0826 | 7.84 | 0.291 | 62.3 % |

Par modèle (horizon de détention) : `gbm_h96` IC 0.0705 (t 9.6), `ridge_h96` IC 0.0681 (t 8.7).

IC réalisé par année : 2023 : 0.0122, 2024 : 0.0526, 2025 : 0.0753, 2026 : 0.0477.

Modèle de direction du marché : corrélation 0.0514 (t ≈ 1.7), par année {2023: -0.0119, 2024: 0.1027, 2025: 0.0909, 2026: 0.0256} ; porte propre non franchie. Sharpe du livre avec exposition nette pilotée : 1.00 (utilisé en production : non).

## Performance nette du portefeuille (bêta-neutre, maker d'abord)

| Mesure | Valeur |
|---|---:|
| Sharpe annualisé (quotidien) | 0.74 |
| Intervalle bootstrap 90 % du Sharpe | [-0.25 ; 1.85] |
| Rendement annualisé (CAGR) | 11.0 % |
| Volatilité annualisée | 14.1 % |
| Perte maximale (drawdown) | -20.3 % |
| Calmar | 0.54 |
| Pire / meilleur jour | -3.84 % / 3.28 % |
| Jours positifs | 51.6 % |
| PnL brut annuel (avant coûts) | 23.5 % |
| Frais / spread / impact annuels | 6.2 % / 2.2 % / 0.8 % |
| Funding annuel (+ = encaissé) | -2.9 % |
| Rotation annuelle (× capital) | 194 |
| Exposition brute / nette / bêta moyennes | 0.61 / 0.12 / 0.01 |
| Positions moyennes | 21.5 |

### Par année

| Année | Rendement | Sharpe | Drawdown max |
|---|---:|---:|---:|
| 2023 | -3.9 % | -1.21 | -7.9 % |
| 2024 | 45.1 % | 2.36 | -8.4 % |
| 2025 | 10.7 % | 0.69 | -14.4 % |
| 2026 | -10.5 % | -1.80 | -20.3 % |

## Tests statistiques

- **Probabilistic Sharpe Ratio** (vrai Sharpe > 0) : 0.915.
- **Deflated Sharpe Ratio** (6 essais effectifs comptés) : 0.534.
- **Nul par permutation** : Sharpe réel au percentile 100 %, p-valeur exacte 0.040 ; 95ᵉ percentile du nul 0.24 (24 répliques).
- **Test SPA de Hansen** (p-valeur, H0 : aucun avantage) : 0.104.
- **PBO** sur la grille : 0.595.
- **Historique minimal** pour conclure à 95 % : 1615 jours.
- **Stress** : coûts ×2 → Sharpe 0.32 ; une barre de latence → Sharpe 0.65.

### Économie du signal sans arrêt (diagnostic, hors porte)

Même stratégie sur toute la période, contrôles de drawdown et de perte journalière désactivés : ce que le signal rapporte et coûte réellement, année par année.

- Sharpe 0.82, CAGR 13.7 %, drawdown max -28.3 % ;
- P&L brut 25.9 %/an contre coûts 8.0 %/an, rotation 167×/an, exposition brute moyenne 0.72 ;
- par année : 2023 : -3.9 % ; 2024 : 45.1 % ; 2025 : 25.0 % ; 2026 : -14.6 %.

### Grille de construction (base du PBO)

| Configuration | Sharpe | CAGR | Drawdown | Rotation |
|---|---:|---:|---:|---:|
| `{'holding_horizon': 32, 'cost_aversion': 0.5}` | 1.05 | 17.2 % | -20.9 % | 383 |
| `{'holding_horizon': 32, 'cost_aversion': 1.0}` | 0.79 | 12.4 % | -20.4 % | 302 |
| `{'holding_horizon': 32, 'cost_aversion': 2.0}` | 0.66 | 9.1 % | -18.8 % | 219 |
| `{'holding_horizon': 96, 'cost_aversion': 0.5}` | 0.96 | 15.2 % | -21.4 % | 269 |
| `{'holding_horizon': 96, 'cost_aversion': 1.0}` | 0.93 | 14.7 % | -21.1 % | 237 |
| `{'holding_horizon': 96, 'cost_aversion': 2.0}` | 0.74 | 11.0 % | -20.3 % | 194 |
| `{'holding_horizon': 192, 'cost_aversion': 0.5}` | 0.75 | 11.1 % | -22.2 % | 215 |
| `{'holding_horizon': 192, 'cost_aversion': 1.0}` | 0.81 | 12.1 % | -21.9 % | 198 |
| `{'holding_horizon': 192, 'cost_aversion': 2.0}` | 0.77 | 11.4 % | -21.5 % | 169 |

## Variables les plus utilisées (gain LightGBM, moyenne des plis)

`amihud_long` 144.3 %, `cs_vol_level` 132.8 %, `log_dollar_volume` 96.0 %, `trade_size` 73.1 %, `volvol_long` 65.6 %, `beta` 62.5 %, `cs_amihud_long` 58.2 %, `kurt_long` 56.7 %, `ivol_share` 53.0 %, `cs_beta` 50.7 %, `mkt_funding` 49.2 %, `skew_long` 45.1 %, `mkt_vol` 44.4 %, `dow_cos` 40.0 %, `cs_funding_sum_4320m` 38.8 %

## Lecture honnête

Un backtest, même hors échantillon, reste une estimation : l'intervalle de confiance du Sharpe ci-dessus dit à quel point. La porte de promotion est volontairement sévère ; un résultat qui ne la franchit pas ne trade pas en réel, quel que soit l'attrait des chiffres. Un résultat qui la franchit démarre en papier, puis en réel à capital réduit, et reste surveillé par l'IC réalisé en continu.
