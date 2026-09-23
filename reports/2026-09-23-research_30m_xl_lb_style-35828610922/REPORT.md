# Rapport de recherche Hermes

*Généré le 2026-09-23T08:02:36+00:00 · configuration `373ceb4d9ce5` · 4313 s de calcul.*

## Verdict : ⛔ NON PROMU — interdit de capital réel

Toutes les mesures ci-dessous sont **hors échantillon** (walk-forward : chaque prédiction vient d'un modèle entraîné uniquement sur le passé) et **nettes de frais, spread, impact et funding**.

| Porte | Valeur | Seuil | |
|---|---:|---:|:-:|
| Sharpe dégonflé (DSR) — probabilité que le vrai Sharpe > le meilleur hasard parmi les essais | 0.110 | ≥ 0.95 | ❌ |
| p-valeur exacte face au nul (mêmes scores permutés entre contrats par blocs d'une semaine) | 0.080 | ≤ 0.05 | ❌ |
| Probabilité de sur-ajustement du backtest (PBO, CSCV sur la grille) | 0.107 | ≤ 0.30 | ✅ |
| Sharpe annualisé net de coûts (quotidien) | 0.338 | ≥ 0.80 | ❌ |
| Part des années civiles positives (années de moins de 90 jours exclues) | 0.750 | ≥ 0.60 | ✅ |
| Mois hors échantillon | 37.105 | ≥ 12.00 | ✅ |
| Sharpe avec coûts doublés | -0.351 | ≥ 0.00 | ❌ |
| Sharpe avec une barre de latence en plus | 0.311 | ≥ 0.00 | ✅ |

## Données

- Source : `binance_archive`, barres `30m`, du 2022-06-01 au 2026-08-31.
- 289 contrats ayant figuré dans l'univers point-in-time (≈ 29 membres en moyenne), 2 149 643 échantillons × 122 variables.
- Hors échantillon à partir du 2023-07-31.

## Qualité de prédiction (IC transversal de Spearman, cible résiduelle nette du funding)

| Horizon | IC moyen | t (Newey-West) | IC/σ | % périodes > 0 |
|---|---:|---:|---:|---:|
| 16 barres | 0.0559 | 11.45 | 0.198 | 57.9 % |
| 48 barres | 0.0718 | 9.04 | 0.253 | 60.4 % |
| 96 barres | 0.0816 | 7.75 | 0.289 | 62.3 % |

Par modèle (horizon de détention) : `gbm_h48` IC 0.0679 (t 9.4), `ridge_h48` IC 0.0663 (t 8.4).

IC réalisé par année : 2023 : 0.0052, 2024 : 0.0593, 2025 : 0.0733, 2026 : 0.0375.

Modèle de direction du marché : corrélation 0.0515 (t ≈ 1.7), par année {2023: -0.0236, 2024: 0.1126, 2025: 0.0801, 2026: 0.0191} ; porte propre non franchie. Sharpe du livre avec exposition nette pilotée : 0.67 (utilisé en production : non).

## Performance nette du portefeuille (bêta-neutre, maker d'abord)

| Mesure | Valeur |
|---|---:|
| Sharpe annualisé (quotidien) | 0.34 |
| Intervalle bootstrap 90 % du Sharpe | [-0.66 ; 1.38] |
| Rendement annualisé (CAGR) | 3.2 % |
| Volatilité annualisée | 9.9 % |
| Perte maximale (drawdown) | -20.7 % |
| Calmar | 0.15 |
| Pire / meilleur jour | -2.57 % / 3.35 % |
| Jours positifs | 50.7 % |
| PnL brut annuel (avant coûts) | 10.7 % |
| Frais / spread / impact annuels | 5.2 % / 2.2 % / 0.7 % |
| Funding annuel (+ = encaissé) | 1.1 % |
| Rotation annuelle (× capital) | 160 |
| Exposition brute / nette / bêta moyennes | 0.37 / -0.00 / 0.01 |
| Positions moyennes | 17.8 |

### Par année

| Année | Rendement | Sharpe | Drawdown max | P&L jambe acheteuse | P&L jambe vendeuse | Funding | Coûts | Rotation |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| 2023 | 1.5 % | 1.31 | -2.3 % | 3.9 % | -2.4 % | 0.1 % | 0.1 % | 2 |
| 2024 | 24.8 % | 1.72 | -10.5 % | 46.0 % | -17.6 % | 3.3 % | 8.6 % | 164 |
| 2025 | -13.7 % | -1.31 | -20.7 % | -13.7 % | 13.4 % | 0.3 % | 14.2 % | 286 |
| 2026 | 0.7 % | 0.28 | -2.7 % | 0.4 % | 3.0 % | -0.4 % | 2.4 % | 44 |

## Tests statistiques

- **Probabilistic Sharpe Ratio** (vrai Sharpe > 0) : 0.735.
- **Deflated Sharpe Ratio** (18 essais effectifs comptés) : 0.110.
- **Nul par permutation** : Sharpe réel au percentile 96 %, p-valeur exacte 0.080 ; 95ᵉ percentile du nul 0.22 (24 répliques).
- **Test SPA de Hansen** (p-valeur, H0 : aucun avantage) : 0.283.
- **PBO** sur la grille : 0.107.
- **Historique minimal** pour conclure à 95 % : 7758 jours.
- **Stress** : coûts ×2 → Sharpe -0.35 ; une barre de latence → Sharpe 0.31.

### Économie du signal sans arrêt (diagnostic, hors porte)

Même stratégie sur toute la période, contrôles de drawdown et de perte journalière désactivés : ce que le signal rapporte et coûte réellement, année par année.

- Sharpe 0.28, CAGR 3.1 %, drawdown max -24.3 % ;
- P&L brut 11.5 %/an contre coûts 8.4 %/an, rotation 162×/an, exposition brute moyenne 0.53 ;
- par année : 2023 : 1.5 % ; 2024 : 25.4 % ; 2025 : -10.2 % ; 2026 : -3.8 %.

### Grille de construction (base du PBO)

| Configuration | Sharpe | CAGR | Drawdown | Rotation |
|---|---:|---:|---:|---:|
| `{'holding_horizon': 16, 'cost_aversion': 0.5}` | 0.17 | 1.4 % | -18.6 % | 353 |
| `{'holding_horizon': 16, 'cost_aversion': 1.0}` | 0.45 | 4.4 % | -19.9 % | 221 |
| `{'holding_horizon': 16, 'cost_aversion': 2.0}` | 0.61 | 5.7 % | -19.7 % | 136 |
| `{'holding_horizon': 48, 'cost_aversion': 0.5}` | 0.05 | -0.0 % | -20.5 % | 282 |
| `{'holding_horizon': 48, 'cost_aversion': 1.0}` | 0.19 | 1.6 % | -20.6 % | 226 |
| `{'holding_horizon': 48, 'cost_aversion': 2.0}` | 0.34 | 3.2 % | -20.7 % | 160 |
| `{'holding_horizon': 96, 'cost_aversion': 0.5}` | -0.35 | -4.1 % | -23.2 % | 249 |
| `{'holding_horizon': 96, 'cost_aversion': 1.0}` | -0.23 | -2.9 % | -23.0 % | 219 |
| `{'holding_horizon': 96, 'cost_aversion': 2.0}` | -0.03 | -0.9 % | -22.0 % | 186 |

## Variables les plus utilisées (gain LightGBM, moyenne des plis)

`amihud_long` 113.9 %, `cs_vol_level` 103.8 %, `log_dollar_volume` 83.7 %, `cs_amihud_long` 55.3 %, `trade_size` 54.7 %, `cs_beta` 51.1 %, `funding_sum_43200m` 49.8 %, `ivol_share` 44.6 %, `beta` 44.1 %, `volvol_long` 44.0 %, `ret_43200m` 43.9 %, `cs_funding_sum_43200m` 43.7 %, `kurt_long` 42.7 %, `mkt_vol` 40.0 %, `iret_43200m` 37.7 %

## Lecture honnête

Un backtest, même hors échantillon, reste une estimation : l'intervalle de confiance du Sharpe ci-dessus dit à quel point. La porte de promotion est volontairement sévère ; un résultat qui ne la franchit pas ne trade pas en réel, quel que soit l'attrait des chiffres. Un résultat qui la franchit démarre en papier, puis en réel à capital réduit, et reste surveillé par l'IC réalisé en continu.
