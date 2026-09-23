# Rapport de recherche Hermes

*Généré le 2026-09-23T05:07:48+00:00 · configuration `1c7541fcd751` · 6578 s de calcul.*

## Verdict : ⛔ NON PROMU — interdit de capital réel

Toutes les mesures ci-dessous sont **hors échantillon** (walk-forward : chaque prédiction vient d'un modèle entraîné uniquement sur le passé) et **nettes de frais, spread, impact et funding**.

| Porte | Valeur | Seuil | |
|---|---:|---:|:-:|
| Sharpe dégonflé (DSR) — probabilité que le vrai Sharpe > le meilleur hasard parmi les essais | 0.014 | ≥ 0.95 | ❌ |
| p-valeur exacte face au nul (mêmes scores permutés entre contrats par blocs d'une semaine) | 0.720 | ≤ 0.05 | ❌ |
| Probabilité de sur-ajustement du backtest (PBO, CSCV sur la grille) | 0.865 | ≤ 0.30 | ❌ |
| Sharpe annualisé net de coûts (quotidien) | -0.233 | ≥ 0.80 | ❌ |
| Part des années civiles positives (années de moins de 90 jours exclues) | 0.250 | ≥ 0.60 | ❌ |
| Mois hors échantillon | 37.105 | ≥ 12.00 | ✅ |
| Sharpe avec coûts doublés | -0.486 | ≥ 0.00 | ❌ |
| Sharpe avec une barre de latence en plus | 0.015 | ≥ 0.00 | ✅ |

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

IC réalisé par année : 2023 : 0.0087, 2024 : 0.0244, 2025 : 0.0244, 2026 : 0.0176.

Modèle de direction du marché : corrélation 0.0170 (t ≈ 2.8), par année {2023: 0.0157, 2024: 0.0385, 2025: 0.001, 2026: 0.0061} ; porte propre franchie. Sharpe du livre avec exposition nette pilotée : 0.60 (utilisé en production : oui).

## Performance nette du portefeuille (bêta-neutre, maker d'abord)

| Mesure | Valeur |
|---|---:|
| Sharpe annualisé (quotidien) | -0.23 |
| Intervalle bootstrap 90 % du Sharpe | [-1.28 ; 0.85] |
| Rendement annualisé (CAGR) | -2.1 % |
| Volatilité annualisée | 7.0 % |
| Perte maximale (drawdown) | -13.6 % |
| Calmar | -0.15 |
| Pire / meilleur jour | -3.88 % / 1.30 % |
| Jours positifs | 52.9 % |
| PnL brut annuel (avant coûts) | -1.0 % |
| Frais / spread / impact annuels | 1.1 % / 0.3 % / 0.3 % |
| Funding annuel (+ = encaissé) | 0.9 % |
| Rotation annuelle (× capital) | 35 |
| Exposition brute / nette / bêta moyennes | 0.24 / 0.02 / 0.02 |
| Positions moyennes | 19.5 |

### Par année

| Année | Rendement | Sharpe | Drawdown max |
|---|---:|---:|---:|
| 2023 | -1.9 % | -0.61 | -6.4 % |
| 2024 | 4.9 % | 0.85 | -5.0 % |
| 2025 | -5.7 % | -0.61 | -11.4 % |
| 2026 | -3.4 % | -1.08 | -6.4 % |

## Tests statistiques

- **Probabilistic Sharpe Ratio** (vrai Sharpe > 0) : 0.319.
- **Deflated Sharpe Ratio** (13 essais effectifs comptés) : 0.014.
- **Nul par permutation** : Sharpe réel au percentile 29 %, p-valeur exacte 0.720 ; 95ᵉ percentile du nul 0.67 (24 répliques).
- **Test SPA de Hansen** (p-valeur, H0 : aucun avantage) : 1.000.
- **PBO** sur la grille : 0.865.
- **Historique minimal** pour conclure à 95 % : — jours.
- **Stress** : coûts ×2 → Sharpe -0.49 ; une barre de latence → Sharpe 0.01.

### Économie du signal sans arrêt (diagnostic, hors porte)

Même stratégie sur toute la période, contrôles de drawdown et de perte journalière désactivés : ce que le signal rapporte et coûte réellement, année par année.

- Sharpe -0.41, CAGR -3.8 %, drawdown max -20.6 % ;
- P&L brut -3.6 %/an contre coûts 1.1 %/an, rotation 23×/an, exposition brute moyenne 0.30 ;
- par année : 2023 : -1.9 % ; 2024 : 4.9 % ; 2025 : -5.9 % ; 2026 : -8.5 %.

### Grille de construction (base du PBO)

| Configuration | Sharpe | CAGR | Drawdown | Rotation |
|---|---:|---:|---:|---:|
| `{'holding_horizon': 2, 'cost_aversion': 0.5}` | 0.10 | 0.6 % | -14.4 % | 122 |
| `{'holding_horizon': 2, 'cost_aversion': 1.0}` | -0.09 | -0.9 % | -13.1 % | 24 |
| `{'holding_horizon': 2, 'cost_aversion': 2.0}` | -0.66 | -3.3 % | -12.3 % | 4 |
| `{'holding_horizon': 4, 'cost_aversion': 0.5}` | 0.11 | 0.7 % | -20.2 % | 178 |
| `{'holding_horizon': 4, 'cost_aversion': 1.0}` | -0.23 | -2.1 % | -13.6 % | 35 |
| `{'holding_horizon': 4, 'cost_aversion': 2.0}` | 0.09 | 0.5 % | -12.7 % | 10 |
| `{'holding_horizon': 8, 'cost_aversion': 0.5}` | -0.10 | -1.6 % | -21.6 % | 264 |
| `{'holding_horizon': 8, 'cost_aversion': 1.0}` | -0.18 | -1.8 % | -18.0 % | 97 |
| `{'holding_horizon': 8, 'cost_aversion': 2.0}` | 0.16 | 0.9 % | -15.9 % | 25 |

## Variables les plus utilisées (gain LightGBM, moyenne des plis)

`cs_premium` 141.0 %, `iret_30m` 116.5 %, `iret_15m` 104.2 %, `iret_60m` 98.9 %, `premium_z` 62.1 %, `trade_size` 40.7 %, `iret_120m` 35.3 %, `hour_cos` 34.6 %, `cs_ret_60m` 32.2 %, `cs_vol_level` 31.3 %, `cs_premium_chg_8h` 30.9 %, `ret_15m` 30.0 %, `cs_ret_120m` 30.0 %, `premium` 29.9 %, `cs_ret_30m` 29.7 %

## Lecture honnête

Un backtest, même hors échantillon, reste une estimation : l'intervalle de confiance du Sharpe ci-dessus dit à quel point. La porte de promotion est volontairement sévère ; un résultat qui ne la franchit pas ne trade pas en réel, quel que soit l'attrait des chiffres. Un résultat qui la franchit démarre en papier, puis en réel à capital réduit, et reste surveillé par l'IC réalisé en continu.
