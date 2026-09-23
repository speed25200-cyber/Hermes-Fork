# Rapport de recherche Hermes

*Généré le 2026-09-23T05:22:47+00:00 · configuration `e4d5812ea662` · 3945 s de calcul.*

## Verdict : ⛔ NON PROMU — interdit de capital réel

Toutes les mesures ci-dessous sont **hors échantillon** (walk-forward : chaque prédiction vient d'un modèle entraîné uniquement sur le passé) et **nettes de frais, spread, impact et funding**.

| Porte | Valeur | Seuil | |
|---|---:|---:|:-:|
| Sharpe dégonflé (DSR) — probabilité que le vrai Sharpe > le meilleur hasard parmi les essais | 0.577 | ≥ 0.95 | ❌ |
| p-valeur exacte face au nul (mêmes scores permutés entre contrats par blocs d'une semaine) | 0.040 | ≤ 0.05 | ✅ |
| Probabilité de sur-ajustement du backtest (PBO, CSCV sur la grille) | 0.476 | ≤ 0.30 | ❌ |
| Sharpe annualisé net de coûts (quotidien) | 0.768 | ≥ 0.80 | ❌ |
| Part des années civiles positives (années de moins de 90 jours exclues) | 0.500 | ≥ 0.60 | ❌ |
| Mois hors échantillon | 37.105 | ≥ 12.00 | ✅ |
| Sharpe avec coûts doublés | 0.451 | ≥ 0.00 | ✅ |
| Sharpe avec une barre de latence en plus | 0.712 | ≥ 0.00 | ✅ |

## Données

- Source : `binance_archive`, barres `30m`, du 2022-06-01 au 2026-08-31.
- 289 contrats ayant figuré dans l'univers point-in-time (≈ 29 membres en moyenne), 2 149 643 échantillons × 114 variables.
- Hors échantillon à partir du 2023-07-31.

## Qualité de prédiction (IC transversal de Spearman, cible résiduelle nette du funding)

| Horizon | IC moyen | t (Newey-West) | IC/σ | % périodes > 0 |
|---|---:|---:|---:|---:|
| 16 barres | 0.0577 | 11.78 | 0.205 | 58.2 % |
| 48 barres | 0.0735 | 9.30 | 0.259 | 60.6 % |
| 96 barres | 0.0828 | 7.84 | 0.293 | 62.6 % |

Par modèle (horizon de détention) : `gbm_h48` IC 0.0711 (t 9.9), `ridge_h48` IC 0.0661 (t 8.4).

IC réalisé par année : 2023 : 0.0125, 2024 : 0.0580, 2025 : 0.0702, 2026 : 0.0424.

Modèle de direction du marché : corrélation 0.0515 (t ≈ 1.7), par année {2023: -0.0236, 2024: 0.1126, 2025: 0.0801, 2026: 0.0191} ; porte propre non franchie. Sharpe du livre avec exposition nette pilotée : 0.98 (utilisé en production : non).

## Performance nette du portefeuille (bêta-neutre, maker d'abord)

| Mesure | Valeur |
|---|---:|
| Sharpe annualisé (quotidien) | 0.77 |
| Intervalle bootstrap 90 % du Sharpe | [-0.26 ; 1.92] |
| Rendement annualisé (CAGR) | 11.9 % |
| Volatilité annualisée | 14.2 % |
| Perte maximale (drawdown) | -19.6 % |
| Calmar | 0.60 |
| Pire / meilleur jour | -3.47 % / 3.20 % |
| Jours positifs | 52.0 % |
| PnL brut annuel (avant coûts) | 22.7 % |
| Frais / spread / impact annuels | 5.2 % / 2.5 % / 0.8 % |
| Funding annuel (+ = encaissé) | -2.0 % |
| Rotation annuelle (× capital) | 164 |
| Exposition brute / nette / bêta moyennes | 0.58 / 0.11 / 0.01 |
| Positions moyennes | 20.0 |

### Par année

| Année | Rendement | Sharpe | Drawdown max |
|---|---:|---:|---:|
| 2023 | -3.7 % | -1.09 | -8.3 % |
| 2024 | 58.0 % | 2.68 | -8.3 % |
| 2025 | 0.5 % | 0.11 | -16.8 % |
| 2026 | -7.6 % | -1.32 | -16.5 % |

## Tests statistiques

- **Probabilistic Sharpe Ratio** (vrai Sharpe > 0) : 0.931.
- **Deflated Sharpe Ratio** (6 essais effectifs comptés) : 0.577.
- **Nul par permutation** : Sharpe réel au percentile 100 %, p-valeur exacte 0.040 ; 95ᵉ percentile du nul 0.34 (24 répliques).
- **Test SPA de Hansen** (p-valeur, H0 : aucun avantage) : 0.098.
- **PBO** sur la grille : 0.476.
- **Historique minimal** pour conclure à 95 % : 1385 jours.
- **Stress** : coûts ×2 → Sharpe 0.45 ; une barre de latence → Sharpe 0.71.

### Économie du signal sans arrêt (diagnostic, hors porte)

Même stratégie sur toute la période, contrôles de drawdown et de perte journalière désactivés : ce que le signal rapporte et coûte réellement, année par année.

- Sharpe 0.66, CAGR 10.9 %, drawdown max -26.7 % ;
- P&L brut 22.1 %/an contre coûts 7.4 %/an, rotation 143×/an, exposition brute moyenne 0.71 ;
- par année : 2023 : -3.7 % ; 2024 : 58.0 % ; 2025 : 6.8 % ; 2026 : -15.2 %.

### Grille de construction (base du PBO)

| Configuration | Sharpe | CAGR | Drawdown | Rotation |
|---|---:|---:|---:|---:|
| `{'holding_horizon': 16, 'cost_aversion': 0.5}` | 1.02 | 17.6 % | -20.6 % | 327 |
| `{'holding_horizon': 16, 'cost_aversion': 1.0}` | 0.79 | 12.7 % | -19.7 % | 255 |
| `{'holding_horizon': 16, 'cost_aversion': 2.0}` | 0.77 | 11.0 % | -18.9 % | 178 |
| `{'holding_horizon': 48, 'cost_aversion': 0.5}` | 0.93 | 15.5 % | -20.6 % | 230 |
| `{'holding_horizon': 48, 'cost_aversion': 1.0}` | 0.88 | 14.5 % | -20.1 % | 202 |
| `{'holding_horizon': 48, 'cost_aversion': 2.0}` | 0.77 | 11.9 % | -19.6 % | 164 |
| `{'holding_horizon': 96, 'cost_aversion': 0.5}` | 0.70 | 10.8 % | -22.0 % | 179 |
| `{'holding_horizon': 96, 'cost_aversion': 1.0}` | 0.71 | 10.7 % | -21.8 % | 159 |
| `{'holding_horizon': 96, 'cost_aversion': 2.0}` | 0.65 | 9.3 % | -22.3 % | 136 |

## Variables les plus utilisées (gain LightGBM, moyenne des plis)

`amihud_long` 139.5 %, `cs_vol_level` 134.7 %, `log_dollar_volume` 110.6 %, `cs_amihud_long` 81.8 %, `trade_size` 71.5 %, `beta` 63.8 %, `volvol_long` 62.3 %, `kurt_long` 55.1 %, `cs_beta` 51.7 %, `dow_cos` 47.4 %, `ivol_share` 46.5 %, `mkt_funding` 46.4 %, `mkt_vol` 42.4 %, `skew_long` 42.2 %, `dvol_surprise_long` 40.9 %

## Lecture honnête

Un backtest, même hors échantillon, reste une estimation : l'intervalle de confiance du Sharpe ci-dessus dit à quel point. La porte de promotion est volontairement sévère ; un résultat qui ne la franchit pas ne trade pas en réel, quel que soit l'attrait des chiffres. Un résultat qui la franchit démarre en papier, puis en réel à capital réduit, et reste surveillé par l'IC réalisé en continu.
