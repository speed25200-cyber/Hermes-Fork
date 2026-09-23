# Rapport de recherche Hermes

*Généré le 2026-09-23T04:45:08+00:00 · configuration `0ed852c2bab5` · 6179 s de calcul.*

## Verdict : ⛔ NON PROMU — interdit de capital réel

Toutes les mesures ci-dessous sont **hors échantillon** (walk-forward : chaque prédiction vient d'un modèle entraîné uniquement sur le passé) et **nettes de frais, spread, impact et funding**.

| Porte | Valeur | Seuil | |
|---|---:|---:|:-:|
| Sharpe dégonflé (DSR) — probabilité que le vrai Sharpe > le meilleur hasard parmi les essais | 0.010 | ≥ 0.95 | ❌ |
| p-valeur exacte face au nul (mêmes scores permutés entre contrats par blocs d'une semaine) | 0.720 | ≤ 0.05 | ❌ |
| Probabilité de sur-ajustement du backtest (PBO, CSCV sur la grille) | 0.786 | ≤ 0.30 | ❌ |
| Sharpe annualisé net de coûts (quotidien) | -0.794 | ≥ 0.80 | ❌ |
| Part des années civiles positives (années de moins de 90 jours exclues) | 0.000 | ≥ 0.60 | ❌ |
| Mois hors échantillon | 13.092 | ≥ 12.00 | ✅ |
| Sharpe avec coûts doublés | -1.660 | ≥ 0.00 | ❌ |
| Sharpe avec une barre de latence en plus | -0.844 | ≥ 0.00 | ❌ |

## Données

- Source : `binance_archive`, barres `1m`, du 2025-04-01 au 2026-08-31.
- 78 contrats ayant figuré dans l'univers point-in-time (≈ 13 membres en moyenne), 9 889 200 échantillons × 112 variables.
- Hors échantillon à partir du 2025-07-30.

## Qualité de prédiction (IC transversal de Spearman, cible résiduelle nette du funding)

| Horizon | IC moyen | t (Newey-West) | IC/σ | % périodes > 0 |
|---|---:|---:|---:|---:|
| 60 barres | 0.0397 | 13.18 | 0.129 | 55.1 % |
| 240 barres | 0.0501 | 9.05 | 0.162 | 56.5 % |
| 480 barres | 0.0527 | 7.04 | 0.169 | 56.8 % |

Par modèle (horizon de détention) : `gbm_h240` IC 0.0441 (t 8.5), `ridge_h240` IC 0.0446 (t 7.7).

IC réalisé par année : 2025 : 0.0393, 2026 : 0.0215.

Modèle de direction du marché : corrélation 0.0424 (t ≈ 2.1), par année {2025: 0.0098, 2026: 0.0711} ; porte propre non franchie. Sharpe du livre avec exposition nette pilotée : -1.04 (utilisé en production : non).

## Performance nette du portefeuille (bêta-neutre, maker d'abord)

| Mesure | Valeur |
|---|---:|
| Sharpe annualisé (quotidien) | -0.79 |
| Intervalle bootstrap 90 % du Sharpe | [-2.68 ; 0.97] |
| Rendement annualisé (CAGR) | -9.1 % |
| Volatilité annualisée | 9.8 % |
| Perte maximale (drawdown) | -18.6 % |
| Calmar | -0.49 |
| Pire / meilleur jour | -3.09 % / 2.96 % |
| Jours positifs | 49.2 % |
| PnL brut annuel (avant coûts) | 5.3 % |
| Frais / spread / impact annuels | 12.7 % / 2.1 % / 0.9 % |
| Funding annuel (+ = encaissé) | 1.4 % |
| Rotation annuelle (× capital) | 398 |
| Exposition brute / nette / bêta moyennes | 0.33 / 0.04 / 0.01 |
| Positions moyennes | 8.3 |

### Par année

| Année | Rendement | Sharpe | Drawdown max |
|---|---:|---:|---:|
| 2025 | -2.8 % | -0.39 | -11.3 % |
| 2026 | -7.2 % | -2.58 | -8.7 % |

## Tests statistiques

- **Probabilistic Sharpe Ratio** (vrai Sharpe > 0) : 0.177.
- **Deflated Sharpe Ratio** (7 essais effectifs comptés) : 0.010.
- **Nul par permutation** : Sharpe réel au percentile 29 %, p-valeur exacte 0.720 ; 95ᵉ percentile du nul 1.14 (24 répliques).
- **Test SPA de Hansen** (p-valeur, H0 : aucun avantage) : 1.000.
- **PBO** sur la grille : 0.786.
- **Historique minimal** pour conclure à 95 % : — jours.
- **Stress** : coûts ×2 → Sharpe -1.66 ; une barre de latence → Sharpe -0.84.

### Économie du signal sans arrêt (diagnostic, hors porte)

Même stratégie sur toute la période, contrôles de drawdown et de perte journalière désactivés : ce que le signal rapporte et coûte réellement, année par année.

- Sharpe -0.25, CAGR -4.0 %, drawdown max -19.5 % ;
- P&L brut 6.8 %/an contre coûts 12.6 %/an, rotation 317×/an, exposition brute moyenne 0.56 ;
- par année : 2025 : -1.2 % ; 2026 : -3.1 %.

### Grille de construction (base du PBO)

| Configuration | Sharpe | CAGR | Drawdown | Rotation |
|---|---:|---:|---:|---:|
| `{'holding_horizon': 60, 'cost_aversion': 0.5}` | -0.78 | -8.6 % | -19.5 % | 466 |
| `{'holding_horizon': 60, 'cost_aversion': 1.0}` | -0.62 | -6.3 % | -16.3 % | 163 |
| `{'holding_horizon': 60, 'cost_aversion': 2.0}` | -0.14 | -1.5 % | -14.3 % | 62 |
| `{'holding_horizon': 240, 'cost_aversion': 0.5}` | -0.74 | -9.4 % | -19.6 % | 597 |
| `{'holding_horizon': 240, 'cost_aversion': 1.0}` | -0.79 | -9.1 % | -18.6 % | 398 |
| `{'holding_horizon': 240, 'cost_aversion': 2.0}` | -0.79 | -7.2 % | -17.0 % | 195 |
| `{'holding_horizon': 480, 'cost_aversion': 0.5}` | -0.46 | -6.5 % | -19.3 % | 496 |
| `{'holding_horizon': 480, 'cost_aversion': 1.0}` | -0.49 | -6.3 % | -18.8 % | 369 |
| `{'holding_horizon': 480, 'cost_aversion': 2.0}` | -0.59 | -6.4 % | -16.4 % | 253 |

## Variables les plus utilisées (gain LightGBM, moyenne des plis)

`trade_size` 136.5 %, `premium_ema_day` 121.4 %, `log_dollar_volume` 115.3 %, `mkt_ret_10080m` 111.9 %, `amihud_long` 104.1 %, `funding_z` 102.2 %, `cs_funding_sum_1440m` 90.4 %, `gk_vol_day` 89.9 %, `funding_last` 85.2 %, `cs_amihud_long` 78.2 %, `cs_range_pos_1440m` 77.5 %, `trend_240m_1440m` 71.0 %, `funding_sum_1440m` 69.5 %, `volvol_long` 68.8 %, `beta` 68.8 %

## Lecture honnête

Un backtest, même hors échantillon, reste une estimation : l'intervalle de confiance du Sharpe ci-dessus dit à quel point. La porte de promotion est volontairement sévère ; un résultat qui ne la franchit pas ne trade pas en réel, quel que soit l'attrait des chiffres. Un résultat qui la franchit démarre en papier, puis en réel à capital réduit, et reste surveillé par l'IC réalisé en continu.
