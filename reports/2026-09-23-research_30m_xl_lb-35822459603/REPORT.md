# Rapport de recherche Hermes

*Généré le 2026-09-23T06:38:40+00:00 · configuration `79035549ca40` · 4246 s de calcul.*

## Verdict : ⛔ NON PROMU — interdit de capital réel

Toutes les mesures ci-dessous sont **hors échantillon** (walk-forward : chaque prédiction vient d'un modèle entraîné uniquement sur le passé) et **nettes de frais, spread, impact et funding**.

| Porte | Valeur | Seuil | |
|---|---:|---:|:-:|
| Sharpe dégonflé (DSR) — probabilité que le vrai Sharpe > le meilleur hasard parmi les essais | 0.546 | ≥ 0.95 | ❌ |
| p-valeur exacte face au nul (mêmes scores permutés entre contrats par blocs d'une semaine) | 0.040 | ≤ 0.05 | ✅ |
| Probabilité de sur-ajustement du backtest (PBO, CSCV sur la grille) | 0.857 | ≤ 0.30 | ❌ |
| Sharpe annualisé net de coûts (quotidien) | 0.928 | ≥ 0.80 | ✅ |
| Part des années civiles positives (années de moins de 90 jours exclues) | 0.500 | ≥ 0.60 | ❌ |
| Mois hors échantillon | 37.105 | ≥ 12.00 | ✅ |
| Sharpe avec coûts doublés | 0.620 | ≥ 0.00 | ✅ |
| Sharpe avec une barre de latence en plus | 0.894 | ≥ 0.00 | ✅ |

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

Modèle de direction du marché : corrélation 0.0515 (t ≈ 1.7), par année {2023: -0.0236, 2024: 0.1126, 2025: 0.0801, 2026: 0.0191} ; porte propre non franchie. Sharpe du livre avec exposition nette pilotée : 1.24 (utilisé en production : non).

## Performance nette du portefeuille (bêta-neutre, maker d'abord)

| Mesure | Valeur |
|---|---:|
| Sharpe annualisé (quotidien) | 0.93 |
| Intervalle bootstrap 90 % du Sharpe | [-0.08 ; 2.15] |
| Rendement annualisé (CAGR) | 15.1 % |
| Volatilité annualisée | 14.3 % |
| Perte maximale (drawdown) | -18.4 % |
| Calmar | 0.82 |
| Pire / meilleur jour | -3.83 % / 3.48 % |
| Jours positifs | 50.5 % |
| PnL brut annuel (avant coûts) | 24.8 % |
| Frais / spread / impact annuels | 4.8 % / 2.3 % / 0.7 % |
| Funding annuel (+ = encaissé) | -1.9 % |
| Rotation annuelle (× capital) | 149 |
| Exposition brute / nette / bêta moyennes | 0.56 / 0.11 / 0.01 |
| Positions moyennes | 20.1 |

### Par année

| Année | Rendement | Sharpe | Drawdown max | P&L jambe acheteuse | P&L jambe vendeuse | Funding | Coûts | Rotation |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| 2023 | -5.4 % | -2.51 | -6.9 % | 2.2 % | -6.8 % | -0.9 % | 0.1 % | 2 |
| 2024 | 69.4 % | 3.13 | -9.0 % | 58.5 % | 2.0 % | 1.4 % | 7.7 % | 149 |
| 2025 | 8.5 % | 0.57 | -14.7 % | 2.8 % | 22.6 % | -3.3 % | 12.4 % | 241 |
| 2026 | -11.1 % | -2.02 | -17.7 % | -5.8 % | 1.2 % | -3.2 % | 3.8 % | 69 |

## Tests statistiques

- **Probabilistic Sharpe Ratio** (vrai Sharpe > 0) : 0.964.
- **Deflated Sharpe Ratio** (13 essais effectifs comptés) : 0.546.
- **Nul par permutation** : Sharpe réel au percentile 100 %, p-valeur exacte 0.040 ; 95ᵉ percentile du nul 0.16 (24 répliques).
- **Test SPA de Hansen** (p-valeur, H0 : aucun avantage) : 0.062.
- **PBO** sur la grille : 0.857.
- **Historique minimal** pour conclure à 95 % : 940 jours.
- **Stress** : coûts ×2 → Sharpe 0.62 ; une barre de latence → Sharpe 0.89.

### Économie du signal sans arrêt (diagnostic, hors porte)

Même stratégie sur toute la période, contrôles de drawdown et de perte journalière désactivés : ce que le signal rapporte et coûte réellement, année par année.

- Sharpe 0.94, CAGR 16.3 %, drawdown max -23.8 % ;
- P&L brut 25.9 %/an contre coûts 6.9 %/an, rotation 131×/an, exposition brute moyenne 0.66 ;
- par année : 2023 : -5.4 % ; 2024 : 69.3 % ; 2025 : 16.3 % ; 2026 : -14.3 %.

### Grille de construction (base du PBO)

| Configuration | Sharpe | CAGR | Drawdown | Rotation |
|---|---:|---:|---:|---:|
| `{'holding_horizon': 16, 'cost_aversion': 0.5}` | 1.05 | 18.4 % | -20.2 % | 297 |
| `{'holding_horizon': 16, 'cost_aversion': 1.0}` | 0.91 | 15.6 % | -19.4 % | 232 |
| `{'holding_horizon': 16, 'cost_aversion': 2.0}` | 0.95 | 14.1 % | -19.0 % | 163 |
| `{'holding_horizon': 48, 'cost_aversion': 0.5}` | 1.08 | 18.4 % | -20.4 % | 211 |
| `{'holding_horizon': 48, 'cost_aversion': 1.0}` | 1.02 | 17.2 % | -19.8 % | 184 |
| `{'holding_horizon': 48, 'cost_aversion': 2.0}` | 0.93 | 15.1 % | -18.4 % | 149 |
| `{'holding_horizon': 96, 'cost_aversion': 0.5}` | 0.94 | 15.3 % | -22.1 % | 170 |
| `{'holding_horizon': 96, 'cost_aversion': 1.0}` | 0.93 | 15.0 % | -21.7 % | 155 |
| `{'holding_horizon': 96, 'cost_aversion': 2.0}` | 0.96 | 15.3 % | -21.2 % | 133 |

## Variables les plus utilisées (gain LightGBM, moyenne des plis)

`amihud_long` 113.9 %, `cs_vol_level` 103.8 %, `log_dollar_volume` 83.7 %, `cs_amihud_long` 55.3 %, `trade_size` 54.7 %, `cs_beta` 51.1 %, `funding_sum_43200m` 49.8 %, `ivol_share` 44.6 %, `beta` 44.1 %, `volvol_long` 44.0 %, `ret_43200m` 43.9 %, `cs_funding_sum_43200m` 43.7 %, `kurt_long` 42.7 %, `mkt_vol` 40.0 %, `iret_43200m` 37.7 %

## Lecture honnête

Un backtest, même hors échantillon, reste une estimation : l'intervalle de confiance du Sharpe ci-dessus dit à quel point. La porte de promotion est volontairement sévère ; un résultat qui ne la franchit pas ne trade pas en réel, quel que soit l'attrait des chiffres. Un résultat qui la franchit démarre en papier, puis en réel à capital réduit, et reste surveillé par l'IC réalisé en continu.
