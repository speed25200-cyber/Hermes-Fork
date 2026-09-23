# Rapport de recherche Hermes

*Généré le 2026-09-23T07:57:50+00:00 · configuration `eee6a3faa06d` · 4018 s de calcul.*

## Verdict : ⛔ NON PROMU — interdit de capital réel

Toutes les mesures ci-dessous sont **hors échantillon** (walk-forward : chaque prédiction vient d'un modèle entraîné uniquement sur le passé) et **nettes de frais, spread, impact et funding**.

| Porte | Valeur | Seuil | |
|---|---:|---:|:-:|
| Sharpe dégonflé (DSR) — probabilité que le vrai Sharpe > le meilleur hasard parmi les essais | 0.486 | ≥ 0.95 | ❌ |
| p-valeur exacte face au nul (mêmes scores permutés entre contrats par blocs d'une semaine) | 0.040 | ≤ 0.05 | ✅ |
| Probabilité de sur-ajustement du backtest (PBO, CSCV sur la grille) | 0.651 | ≤ 0.30 | ❌ |
| Sharpe annualisé net de coûts (quotidien) | 0.913 | ≥ 0.80 | ✅ |
| Part des années civiles positives (années de moins de 90 jours exclues) | 0.500 | ≥ 0.60 | ❌ |
| Mois hors échantillon | 37.105 | ≥ 12.00 | ✅ |
| Sharpe avec coûts doublés | 0.591 | ≥ 0.00 | ✅ |
| Sharpe avec une barre de latence en plus | 0.880 | ≥ 0.00 | ✅ |

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

Modèle de direction du marché : corrélation 0.0515 (t ≈ 1.7), par année {2023: -0.0236, 2024: 0.1126, 2025: 0.0801, 2026: 0.0191} ; porte propre non franchie. Sharpe du livre avec exposition nette pilotée : 1.09 (utilisé en production : non).

## Performance nette du portefeuille (bêta-neutre, maker d'abord)

| Mesure | Valeur |
|---|---:|
| Sharpe annualisé (quotidien) | 0.91 |
| Intervalle bootstrap 90 % du Sharpe | [-0.09 ; 2.10] |
| Rendement annualisé (CAGR) | 14.6 % |
| Volatilité annualisée | 14.2 % |
| Perte maximale (drawdown) | -18.4 % |
| Calmar | 0.79 |
| Pire / meilleur jour | -3.80 % / 3.49 % |
| Jours positifs | 50.9 % |
| PnL brut annuel (avant coûts) | 24.8 % |
| Frais / spread / impact annuels | 5.0 % / 2.5 % / 0.9 % |
| Funding annuel (+ = encaissé) | -1.8 % |
| Rotation annuelle (× capital) | 155 |
| Exposition brute / nette / bêta moyennes | 0.54 / 0.11 / 0.01 |
| Positions moyennes | 18.9 |

### Par année

| Année | Rendement | Sharpe | Drawdown max | P&L jambe acheteuse | P&L jambe vendeuse | Funding | Coûts | Rotation |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| 2023 | -4.5 % | -2.28 | -5.9 % | 1.2 % | -4.9 % | -0.6 % | 0.2 % | 3 |
| 2024 | 65.3 % | 3.00 | -9.1 % | 57.3 % | 1.4 % | 1.5 % | 8.4 % | 156 |
| 2025 | 9.5 % | 0.63 | -15.4 % | 1.3 % | 25.7 % | -3.3 % | 13.2 % | 249 |
| 2026 | -11.9 % | -2.18 | -18.3 % | -5.8 % | 0.7 % | -3.2 % | 4.1 % | 72 |

## Tests statistiques

- **Probabilistic Sharpe Ratio** (vrai Sharpe > 0) : 0.960.
- **Deflated Sharpe Ratio** (16 essais effectifs comptés) : 0.486.
- **Nul par permutation** : Sharpe réel au percentile 100 %, p-valeur exacte 0.040 ; 95ᵉ percentile du nul 0.18 (24 répliques).
- **Test SPA de Hansen** (p-valeur, H0 : aucun avantage) : 0.067.
- **PBO** sur la grille : 0.651.
- **Historique minimal** pour conclure à 95 % : 997 jours.
- **Stress** : coûts ×2 → Sharpe 0.59 ; une barre de latence → Sharpe 0.88.

### Économie du signal sans arrêt (diagnostic, hors porte)

Même stratégie sur toute la période, contrôles de drawdown et de perte journalière désactivés : ce que le signal rapporte et coûte réellement, année par année.

- Sharpe 0.91, CAGR 15.3 %, drawdown max -23.9 % ;
- P&L brut 25.3 %/an contre coûts 7.4 %/an, rotation 137×/an, exposition brute moyenne 0.64 ;
- par année : 2023 : -4.5 % ; 2024 : 65.3 % ; 2025 : 17.0 % ; 2026 : -15.9 %.

### Grille de construction (base du PBO)

| Configuration | Sharpe | CAGR | Drawdown | Rotation |
|---|---:|---:|---:|---:|
| `{'holding_horizon': 16, 'cost_aversion': 0.5}` | 1.11 | 19.3 % | -20.0 % | 308 |
| `{'holding_horizon': 16, 'cost_aversion': 1.0}` | 0.89 | 14.7 % | -19.3 % | 239 |
| `{'holding_horizon': 16, 'cost_aversion': 2.0}` | 0.88 | 12.5 % | -19.1 % | 170 |
| `{'holding_horizon': 48, 'cost_aversion': 0.5}` | 1.03 | 17.2 % | -20.3 % | 219 |
| `{'holding_horizon': 48, 'cost_aversion': 1.0}` | 1.01 | 16.6 % | -19.5 % | 192 |
| `{'holding_horizon': 48, 'cost_aversion': 2.0}` | 0.91 | 14.6 % | -18.4 % | 155 |
| `{'holding_horizon': 96, 'cost_aversion': 0.5}` | 0.87 | 13.8 % | -22.2 % | 177 |
| `{'holding_horizon': 96, 'cost_aversion': 1.0}` | 0.84 | 12.9 % | -21.8 % | 161 |
| `{'holding_horizon': 96, 'cost_aversion': 2.0}` | 0.87 | 13.3 % | -21.2 % | 139 |

## Variables les plus utilisées (gain LightGBM, moyenne des plis)

`amihud_long` 113.9 %, `cs_vol_level` 103.8 %, `log_dollar_volume` 83.7 %, `cs_amihud_long` 55.3 %, `trade_size` 54.7 %, `cs_beta` 51.1 %, `funding_sum_43200m` 49.8 %, `ivol_share` 44.6 %, `beta` 44.1 %, `volvol_long` 44.0 %, `ret_43200m` 43.9 %, `cs_funding_sum_43200m` 43.7 %, `kurt_long` 42.7 %, `mkt_vol` 40.0 %, `iret_43200m` 37.7 %

## Lecture honnête

Un backtest, même hors échantillon, reste une estimation : l'intervalle de confiance du Sharpe ci-dessus dit à quel point. La porte de promotion est volontairement sévère ; un résultat qui ne la franchit pas ne trade pas en réel, quel que soit l'attrait des chiffres. Un résultat qui la franchit démarre en papier, puis en réel à capital réduit, et reste surveillé par l'IC réalisé en continu.
