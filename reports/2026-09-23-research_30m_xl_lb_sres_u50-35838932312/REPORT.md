# Rapport de recherche Hermes

*Généré le 2026-09-23T10:35:25+00:00 · configuration `064c675ee80d` · 6482 s de calcul.*

## Verdict : ⛔ NON PROMU — interdit de capital réel

Toutes les mesures ci-dessous sont **hors échantillon** (walk-forward : chaque prédiction vient d'un modèle entraîné uniquement sur le passé) et **nettes de frais, spread, impact et funding**.

| Porte | Valeur | Seuil | |
|---|---:|---:|:-:|
| Sharpe dégonflé (DSR) — probabilité que le vrai Sharpe > le meilleur hasard parmi les essais | 0.218 | ≥ 0.95 | ❌ |
| p-valeur exacte face au nul (mêmes scores permutés entre contrats par blocs d'une semaine) | 0.040 | ≤ 0.05 | ✅ |
| Probabilité de sur-ajustement du backtest (PBO, CSCV sur la grille) | 0.048 | ≤ 0.30 | ✅ |
| Sharpe annualisé net de coûts (quotidien) | 0.827 | ≥ 0.80 | ✅ |
| Part des années civiles positives (années de moins de 90 jours exclues) | 0.750 | ≥ 0.60 | ✅ |
| Mois hors échantillon | 37.105 | ≥ 12.00 | ✅ |
| Sharpe avec coûts doublés | -0.080 | ≥ 0.00 | ❌ |
| Sharpe avec une barre de latence en plus | 0.819 | ≥ 0.00 | ✅ |

## Données

- Source : `binance_archive`, barres `30m`, du 2022-06-01 au 2026-08-31.
- 419 contrats ayant figuré dans l'univers point-in-time (≈ 48 membres en moyenne), 3 582 665 échantillons × 122 variables.
- Hors échantillon à partir du 2023-07-31.

## Qualité de prédiction (IC transversal de Spearman, cible résiduelle nette du funding)

| Horizon | IC moyen | t (Newey-West) | IC/σ | % périodes > 0 |
|---|---:|---:|---:|---:|
| 16 barres | 0.0487 | 16.27 | 0.299 | 61.7 % |
| 48 barres | 0.0519 | 11.35 | 0.313 | 62.5 % |
| 96 barres | 0.0472 | 8.69 | 0.289 | 61.0 % |

Par modèle (horizon de détention) : `gbm_h48` IC 0.0512 (t 11.6), `ridge_h48` IC 0.0365 (t 8.2).

IC réalisé par année : 2023 : 0.0159, 2024 : 0.0256, 2025 : 0.0344, 2026 : 0.0054.

Modèle de direction du marché : corrélation 0.0686 (t ≈ 2.3), par année {2023: 0.0203, 2024: 0.1355, 2025: 0.0782, 2026: 0.0539} ; porte propre non franchie. Sharpe du livre avec exposition nette pilotée : 1.09 (utilisé en production : non).

## Performance nette du portefeuille (bêta-neutre, maker d'abord)

| Mesure | Valeur |
|---|---:|
| Sharpe annualisé (quotidien) | 0.83 |
| Intervalle bootstrap 90 % du Sharpe | [-0.10 ; 1.95] |
| Rendement annualisé (CAGR) | 11.2 % |
| Volatilité annualisée | 11.7 % |
| Perte maximale (drawdown) | -13.9 % |
| Calmar | 0.81 |
| Pire / meilleur jour | -2.70 % / 8.82 % |
| Jours positifs | 44.1 % |
| PnL brut annuel (avant coûts) | 17.2 % |
| Frais / spread / impact annuels | 5.8 % / 2.5 % / 1.3 % |
| Funding annuel (+ = encaissé) | 3.7 % |
| Rotation annuelle (× capital) | 178 |
| Exposition brute / nette / bêta moyennes | 0.51 / 0.02 / 0.01 |
| Positions moyennes | 27.3 |
| Stops catastrophe déclenchés par an | 192 |

### Par année

| Année | Rendement | Sharpe | Drawdown max | P&L jambe acheteuse | P&L jambe vendeuse | Funding | Coûts | Rotation | Stops |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 2023 | 2.0 % | 0.74 | -6.2 % | 12.0 % | -11.7 % | 2.4 % | 0.6 % | 11 | 68 |
| 2024 | 15.7 % | 1.50 | -13.9 % | 15.2 % | 8.0 % | 2.4 % | 10.6 % | 203 | 106 |
| 2025 | 20.9 % | 1.22 | -11.5 % | -24.5 % | 56.8 % | 4.4 % | 16.3 % | 307 | 254 |
| 2026 | -2.7 % | -0.60 | -6.0 % | -4.9 % | 2.1 % | 2.1 % | 1.9 % | 31 | 165 |

## Tests statistiques

- **Probabilistic Sharpe Ratio** (vrai Sharpe > 0) : 0.961.
- **Deflated Sharpe Ratio** (32 essais effectifs comptés) : 0.218.
- **Nul par permutation** : Sharpe réel au percentile 100 %, p-valeur exacte 0.040 ; 95ᵉ percentile du nul 0.21 (24 répliques).
- **Test SPA de Hansen** (p-valeur, H0 : aucun avantage) : 0.085.
- **PBO** sur la grille : 0.048.
- **Historique minimal** pour conclure à 95 % : 978 jours.
- **Stress** : coûts ×2 → Sharpe -0.08 ; une barre de latence → Sharpe 0.82.

### Économie du signal sans arrêt (diagnostic, hors porte)

Même stratégie sur toute la période, contrôles de drawdown et de perte journalière désactivés : ce que le signal rapporte et coûte réellement, année par année.

- Sharpe 0.96, CAGR 13.7 %, drawdown max -15.3 % ;
- P&L brut 17.6 %/an contre coûts 8.1 %/an, rotation 148×/an, exposition brute moyenne 0.61 ;
- par année : 2023 : 2.0 % ; 2024 : 18.9 % ; 2025 : 25.2 % ; 2026 : -2.1 %.

### Grille de construction (base du PBO)

| Configuration | Sharpe | CAGR | Drawdown | Rotation |
|---|---:|---:|---:|---:|
| `{'holding_horizon': 16, 'cost_aversion': 0.5}` | 1.07 | 19.1 % | -13.9 % | 534 |
| `{'holding_horizon': 16, 'cost_aversion': 1.0}` | 1.55 | 28.4 % | -11.3 % | 306 |
| `{'holding_horizon': 16, 'cost_aversion': 2.0}` | 1.50 | 22.9 % | -9.8 % | 136 |
| `{'holding_horizon': 48, 'cost_aversion': 0.5}` | 0.86 | 15.3 % | -17.1 % | 336 |
| `{'holding_horizon': 48, 'cost_aversion': 1.0}` | 0.99 | 15.7 % | -15.2 % | 273 |
| `{'holding_horizon': 48, 'cost_aversion': 2.0}` | 0.83 | 11.2 % | -13.9 % | 178 |
| `{'holding_horizon': 96, 'cost_aversion': 0.5}` | 0.10 | 0.7 % | -20.6 % | 220 |
| `{'holding_horizon': 96, 'cost_aversion': 1.0}` | 0.14 | 1.1 % | -19.1 % | 211 |
| `{'holding_horizon': 96, 'cost_aversion': 2.0}` | 0.19 | 1.5 % | -17.9 % | 172 |

## Variables les plus utilisées (gain LightGBM, moyenne des plis)

`log_dollar_volume` 78.6 %, `cs_amihud_long` 61.9 %, `amihud_long` 60.1 %, `kurt_long` 56.7 %, `beta` 52.7 %, `funding_sum_10080m` 49.5 %, `funding_sum_43200m` 49.5 %, `cs_funding_sum_43200m` 49.4 %, `cs_vol_level` 48.6 %, `cs_beta` 45.5 %, `skew_long` 41.6 %, `dvol_surprise_long` 40.2 %, `ret_43200m` 39.9 %, `volvol_long` 39.7 %, `dist_low_43200m` 38.6 %

## Lecture honnête

Un backtest, même hors échantillon, reste une estimation : l'intervalle de confiance du Sharpe ci-dessus dit à quel point. La porte de promotion est volontairement sévère ; un résultat qui ne la franchit pas ne trade pas en réel, quel que soit l'attrait des chiffres. Un résultat qui la franchit démarre en papier, puis en réel à capital réduit, et reste surveillé par l'IC réalisé en continu.
