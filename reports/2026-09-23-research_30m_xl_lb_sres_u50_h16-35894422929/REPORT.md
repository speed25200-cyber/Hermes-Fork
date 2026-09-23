# Rapport de recherche Hermes

*Généré le 2026-09-23T18:38:57+00:00 · configuration `5978d584ff75` · 4928 s de calcul.*

## Verdict : ⛔ NON PROMU — interdit de capital réel

Toutes les mesures ci-dessous sont **hors échantillon** (walk-forward : chaque prédiction vient d'un modèle entraîné uniquement sur le passé) et **nettes de frais, spread, impact et funding**.

| Porte | Valeur | Seuil | |
|---|---:|---:|:-:|
| Sharpe dégonflé (DSR) — probabilité que le vrai Sharpe > le meilleur hasard parmi les essais | 0.437 | ≥ 0.95 | ❌ |
| p-valeur exacte face au nul (mêmes scores permutés entre contrats par blocs d'une semaine) | 0.080 | ≤ 0.05 | ❌ |
| Probabilité de sur-ajustement du backtest (PBO, CSCV sur la grille) | 0.083 | ≤ 0.30 | ✅ |
| Sharpe annualisé net de coûts (quotidien) | 1.088 | ≥ 0.80 | ✅ |
| Part des années civiles positives (années de moins de 90 jours exclues) | 0.500 | ≥ 0.60 | ❌ |
| Mois hors échantillon | 37.105 | ≥ 12.00 | ✅ |
| Sharpe avec coûts doublés | 0.555 | ≥ 0.00 | ✅ |
| Sharpe avec une barre de latence en plus | 1.018 | ≥ 0.00 | ✅ |
| Sharpe avec stops exécutés au pire (plus bas / plus haut de la bougie) | 0.333 | ≥ 0.00 | ✅ |

## Données

- Source : `binance_archive`, barres `30m`, du 2022-06-01 au 2026-08-31.
- 265 contrats ayant figuré dans l'univers point-in-time (≈ 48 membres en moyenne), 3 582 582 échantillons × 122 variables.
- Hors échantillon à partir du 2023-07-31.

## Qualité de prédiction (IC transversal de Spearman, cible résiduelle nette du funding)

| Horizon | IC moyen | t (Newey-West) | IC/σ | % périodes > 0 |
|---|---:|---:|---:|---:|
| 16 barres | 0.0508 | 16.50 | 0.304 | 61.9 % |
| 48 barres | 0.0529 | 11.35 | 0.316 | 62.3 % |
| 96 barres | 0.0459 | 8.29 | 0.275 | 60.6 % |

Par modèle (horizon de détention) : `gbm_h16` IC 0.0477 (t 15.3), `ridge_h16` IC 0.0412 (t 14.8).

IC réalisé par année : 2023 : 0.0065, 2024 : 0.0183, 2025 : 0.0405, 2026 : 0.0091.

Modèle de direction du marché : corrélation 0.0257 (t ≈ 1.5), par année {2023: 0.0111, 2024: 0.069, 2025: 0.0251, 2026: 0.0143} ; porte propre non franchie. Sharpe du livre avec exposition nette pilotée : 1.23 (utilisé en production : non).

## Performance nette du portefeuille (bêta-neutre, maker d'abord)

| Mesure | Valeur |
|---|---:|
| Sharpe annualisé (quotidien) | 1.09 |
| Intervalle bootstrap 90 % du Sharpe | [0.14 ; 2.12] |
| Rendement annualisé (CAGR) | 12.7 % |
| Volatilité annualisée | 11.2 % |
| Perte maximale (drawdown) | -16.1 % |
| Calmar | 0.78 |
| Pire / meilleur jour | -2.92 % / 3.02 % |
| Jours positifs | 49.2 % |
| PnL brut annuel (avant coûts) | 18.8 % |
| Frais / spread / impact annuels | 5.3 % / 1.9 % / 1.4 % |
| Funding annuel (+ = encaissé) | 2.3 % |
| Rotation annuelle (× capital) | 164 |
| Exposition brute / nette / bêta moyennes | 0.52 / 0.02 / 0.01 |
| Positions moyennes | 29.6 |
| Stops catastrophe déclenchés par an | 63 |

### Par année

| Année | Rendement | Sharpe | Drawdown max | P&L jambe acheteuse | P&L jambe vendeuse | Funding | Coûts | Rotation | Stops |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 2023 | -2.5 % | -1.59 | -4.5 % | 6.3 % | -9.6 % | 1.0 % | 0.2 % | 3 | 31 |
| 2024 | 20.2 % | 2.37 | -5.7 % | -5.5 % | 24.9 % | 1.8 % | 2.8 % | 56 | 58 |
| 2025 | 31.5 % | 1.66 | -13.4 % | -12.4 % | 56.9 % | 2.9 % | 18.5 % | 354 | 74 |
| 2026 | -6.2 % | -1.74 | -9.1 % | 3.8 % | -6.6 % | 1.3 % | 4.8 % | 93 | 31 |

## Tests statistiques

- **Probabilistic Sharpe Ratio** (vrai Sharpe > 0) : 0.976.
- **Deflated Sharpe Ratio** (36 essais effectifs comptés) : 0.437.
- **Nul par permutation** : Sharpe réel au percentile 96 %, p-valeur exacte 0.080 ; 95ᵉ percentile du nul 0.96 (24 répliques).
- **Test SPA de Hansen** (p-valeur, H0 : aucun avantage) : 0.034.
- **PBO** sur la grille : 0.083.
- **Historique minimal** pour conclure à 95 % : 779 jours.
- **Stress** : coûts ×2 → Sharpe 0.55 ; une barre de latence → Sharpe 1.02 ; stops exécutés au pire → Sharpe 0.33.
- **Concentration** : sans ses 5 meilleurs jours, Sharpe 0.78 et rendement annualisé 8.1 %.
- **Sans stops catastrophe** (diagnostic) : Sharpe 1.01, rendement annualisé 11.5 %, drawdown max -16.2 %.

### Économie du signal sans arrêt (diagnostic, hors porte)

Même stratégie sur toute la période, contrôles de drawdown et de perte journalière désactivés : ce que le signal rapporte et coûte réellement, année par année.

- Sharpe 0.75, CAGR 8.9 %, drawdown max -23.1 % ;
- P&L brut 14.3 %/an contre coûts 7.2 %/an, rotation 136×/an, exposition brute moyenne 0.64 ;
- par année : 2023 : -2.5 % ; 2024 : 20.2 % ; 2025 : 30.2 % ; 2026 : -14.8 %.

### Grille de construction (base du PBO)

| Configuration | Sharpe | CAGR | Drawdown | Rotation |
|---|---:|---:|---:|---:|
| `{'holding_horizon': 16, 'cost_aversion': 0.5}` | 0.69 | 9.4 % | -16.9 % | 588 |
| `{'holding_horizon': 16, 'cost_aversion': 1.0}` | 1.09 | 14.9 % | -16.6 % | 355 |
| `{'holding_horizon': 16, 'cost_aversion': 2.0}` | 1.09 | 12.7 % | -16.1 % | 164 |
| `{'holding_horizon': 48, 'cost_aversion': 0.5}` | 0.45 | 5.3 % | -18.3 % | 383 |
| `{'holding_horizon': 48, 'cost_aversion': 1.0}` | 0.47 | 5.4 % | -16.6 % | 293 |
| `{'holding_horizon': 48, 'cost_aversion': 2.0}` | 0.75 | 9.2 % | -13.8 % | 162 |
| `{'holding_horizon': 96, 'cost_aversion': 0.5}` | -0.05 | -1.1 % | -19.8 % | 285 |
| `{'holding_horizon': 96, 'cost_aversion': 1.0}` | 0.01 | -0.4 % | -18.7 % | 259 |
| `{'holding_horizon': 96, 'cost_aversion': 2.0}` | -0.19 | -2.3 % | -17.5 % | 199 |

## Variables les plus utilisées (gain LightGBM, moyenne des plis)

`log_dollar_volume` 72.9 %, `amihud_long` 68.0 %, `cs_amihud_long` 66.7 %, `cs_beta` 62.2 %, `beta` 57.0 %, `funding_sum_43200m` 54.5 %, `cs_funding_sum_43200m` 51.5 %, `cs_vol_level` 50.7 %, `kurt_long` 44.0 %, `cs_trend_1440m_5760m` 41.2 %, `volvol_long` 40.7 %, `dist_low_43200m` 40.5 %, `funding_sum_10080m` 40.3 %, `cs_range_pos_43200m` 38.5 %, `trade_size` 37.4 %

## Lecture honnête

Un backtest, même hors échantillon, reste une estimation : l'intervalle de confiance du Sharpe ci-dessus dit à quel point. La porte de promotion est volontairement sévère ; un résultat qui ne la franchit pas ne trade pas en réel, quel que soit l'attrait des chiffres. Un résultat qui la franchit démarre en papier, puis en réel à capital réduit, et reste surveillé par l'IC réalisé en continu.
