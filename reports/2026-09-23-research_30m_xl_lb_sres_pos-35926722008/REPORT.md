# Rapport de recherche Hermes

*Généré le 2026-09-24T00:47:27+00:00 · configuration `fffd0c17d2d6` · 9369 s de calcul.*

## Verdict : ⛔ NON PROMU — interdit de capital réel

Toutes les mesures ci-dessous sont **hors échantillon** (walk-forward : chaque prédiction vient d'un modèle entraîné uniquement sur le passé) et **nettes de frais, spread, impact et funding**.

| Porte | Valeur | Seuil | |
|---|---:|---:|:-:|
| Sharpe dégonflé (DSR) — probabilité que le vrai Sharpe > le meilleur hasard parmi les essais | 0.248 | ≥ 0.95 | ❌ |
| p-valeur exacte face au nul (mêmes scores permutés entre contrats par blocs d'une semaine) | 0.040 | ≤ 0.05 | ✅ |
| Probabilité de sur-ajustement du backtest (PBO, CSCV sur la grille) | 0.040 | ≤ 0.30 | ✅ |
| Sharpe annualisé net de coûts (quotidien) | 0.786 | ≥ 0.80 | ❌ |
| Part des années civiles positives (années de moins de 90 jours exclues) | 0.500 | ≥ 0.60 | ❌ |
| Mois hors échantillon | 37.105 | ≥ 12.00 | ✅ |
| Sharpe avec coûts doublés | 0.157 | ≥ 0.00 | ✅ |
| Sharpe avec une barre de latence en plus | 0.642 | ≥ 0.00 | ✅ |
| Sharpe avec stops exécutés au pire (plus bas / plus haut de la bougie) | 0.044 | ≥ 0.00 | ✅ |

## Données

- Source : `binance_archive`, barres `30m`, du 2022-06-01 au 2026-08-31.
- 198 contrats ayant figuré dans l'univers point-in-time (≈ 29 membres en moyenne), 2 149 680 échantillons × 125 variables.
- Hors échantillon à partir du 2023-07-31.

## Qualité de prédiction (IC transversal de Spearman, cible résiduelle nette du funding)

| Horizon | IC moyen | t (Newey-West) | IC/σ | % périodes > 0 |
|---|---:|---:|---:|---:|
| 16 barres | 0.0464 | 13.85 | 0.234 | 59.4 % |
| 48 barres | 0.0475 | 9.16 | 0.238 | 59.8 % |
| 96 barres | 0.0426 | 6.55 | 0.214 | 58.1 % |

Par modèle (horizon de détention) : `gbm_h48` IC 0.0447 (t 8.9), `ridge_h48` IC 0.0366 (t 7.3).

IC réalisé par année : 2023 : 0.0115, 2024 : 0.0258, 2025 : 0.0408, 2026 : -0.0166.

Modèle de direction du marché : corrélation 0.0451 (t ≈ 1.5), par année {2023: 0.0049, 2024: 0.113, 2025: 0.0486, 2026: 0.0227} ; porte propre non franchie. Sharpe du livre avec exposition nette pilotée : 1.13 (utilisé en production : non).

## Performance nette du portefeuille (bêta-neutre, maker d'abord)

| Mesure | Valeur |
|---|---:|
| Sharpe annualisé (quotidien) | 0.79 |
| Intervalle bootstrap 90 % du Sharpe | [-0.11 ; 1.86] |
| Rendement annualisé (CAGR) | 8.3 % |
| Volatilité annualisée | 9.5 % |
| Perte maximale (drawdown) | -14.9 % |
| Calmar | 0.56 |
| Pire / meilleur jour | -2.57 % / 3.43 % |
| Jours positifs | 43.9 % |
| PnL brut annuel (avant coûts) | 13.0 % |
| Frais / spread / impact annuels | 4.1 % / 1.6 % / 0.7 % |
| Funding annuel (+ = encaissé) | 1.8 % |
| Rotation annuelle (× capital) | 128 |
| Exposition brute / nette / bêta moyennes | 0.36 / 0.02 / 0.01 |
| Positions moyennes | 16.2 |
| Stops catastrophe déclenchés par an | 29 |

### Par année

| Année | Rendement | Sharpe | Drawdown max | P&L jambe acheteuse | P&L jambe vendeuse | Funding | Coûts | Rotation | Stops |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 2023 | -1.2 % | -0.42 | -7.1 % | 8.3 % | -11.2 % | 2.2 % | 0.4 % | 8 | 20 |
| 2024 | 7.9 % | 0.89 | -14.4 % | 12.0 % | 0.9 % | 1.2 % | 6.0 % | 124 | 17 |
| 2025 | 26.3 % | 1.80 | -7.2 % | 2.1 % | 33.6 % | 1.6 % | 13.2 % | 262 | 35 |
| 2026 | -5.1 % | -3.24 | -6.1 % | -2.1 % | -3.6 % | 0.5 % | 0.1 % | 2 | 19 |

## Tests statistiques

- **Probabilistic Sharpe Ratio** (vrai Sharpe > 0) : 0.933.
- **Deflated Sharpe Ratio** (38 essais effectifs comptés) : 0.248.
- **Nul par permutation** : Sharpe réel au percentile 100 %, p-valeur exacte 0.040 ; 95ᵉ percentile du nul 0.43 (24 répliques).
- **Test SPA de Hansen** (p-valeur, H0 : aucun avantage) : 0.094.
- **PBO** sur la grille : 0.040.
- **Historique minimal** pour conclure à 95 % : 1352 jours.
- **Stress** : coûts ×2 → Sharpe 0.16 ; une barre de latence → Sharpe 0.64 ; stops exécutés au pire → Sharpe 0.04.
- **Concentration** : sans ses 5 meilleurs jours, Sharpe 0.45 et rendement annualisé 3.8 %.
- **Sans stops catastrophe** (diagnostic) : Sharpe 0.91, rendement annualisé 9.6 %, drawdown max -12.3 %.

### Économie du signal sans arrêt (diagnostic, hors porte)

Même stratégie sur toute la période, contrôles de drawdown et de perte journalière désactivés : ce que le signal rapporte et coûte réellement, année par année.

- Sharpe 0.97, CAGR 11.1 %, drawdown max -14.6 % ;
- P&L brut 14.3 %/an contre coûts 5.4 %/an, rotation 105×/an, exposition brute moyenne 0.47 ;
- par année : 2023 : -1.2 % ; 2024 : 12.6 % ; 2025 : 31.1 % ; 2026 : -5.1 %.

### Grille de construction (base du PBO)

| Configuration | Sharpe | CAGR | Drawdown | Rotation |
|---|---:|---:|---:|---:|
| `{'holding_horizon': 16, 'cost_aversion': 0.5}` | 1.69 | 25.1 % | -13.3 % | 335 |
| `{'holding_horizon': 16, 'cost_aversion': 1.0}` | 1.61 | 22.4 % | -12.5 % | 215 |
| `{'holding_horizon': 16, 'cost_aversion': 2.0}` | 0.99 | 11.1 % | -14.1 % | 106 |
| `{'holding_horizon': 48, 'cost_aversion': 0.5}` | 1.14 | 14.7 % | -17.0 % | 224 |
| `{'holding_horizon': 48, 'cost_aversion': 1.0}` | 1.01 | 11.7 % | -16.0 % | 185 |
| `{'holding_horizon': 48, 'cost_aversion': 2.0}` | 0.79 | 8.3 % | -14.9 % | 128 |
| `{'holding_horizon': 96, 'cost_aversion': 0.5}` | 0.67 | 6.9 % | -18.8 % | 172 |
| `{'holding_horizon': 96, 'cost_aversion': 1.0}` | 0.60 | 5.9 % | -17.9 % | 154 |
| `{'holding_horizon': 96, 'cost_aversion': 2.0}` | 0.43 | 3.9 % | -17.6 % | 121 |

## Variables les plus utilisées (gain LightGBM, moyenne des plis)

`log_dollar_volume` 80.6 %, `amihud_long` 72.6 %, `funding_sum_43200m` 69.3 %, `volvol_long` 57.5 %, `beta` 51.3 %, `cs_funding_sum_43200m` 50.8 %, `ls_top_z` 50.1 %, `cs_amihud_long` 49.2 %, `funding_sum_10080m` 48.3 %, `cs_funding_sum_10080m` 47.8 %, `skew_long` 47.8 %, `kurt_long` 44.1 %, `cs_beta` 39.9 %, `trade_size` 39.3 %, `dist_low_43200m` 39.0 %

## Lecture honnête

Un backtest, même hors échantillon, reste une estimation : l'intervalle de confiance du Sharpe ci-dessus dit à quel point. La porte de promotion est volontairement sévère ; un résultat qui ne la franchit pas ne trade pas en réel, quel que soit l'attrait des chiffres. Un résultat qui la franchit démarre en papier, puis en réel à capital réduit, et reste surveillé par l'IC réalisé en continu.
