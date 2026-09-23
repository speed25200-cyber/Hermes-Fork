# Rapport de recherche Hermes

*Généré le 2026-09-23T16:38:39+00:00 · configuration `5ab1de1eb2a5` · 6326 s de calcul.*

## Verdict : ⛔ NON PROMU — interdit de capital réel

Toutes les mesures ci-dessous sont **hors échantillon** (walk-forward : chaque prédiction vient d'un modèle entraîné uniquement sur le passé) et **nettes de frais, spread, impact et funding**.

| Porte | Valeur | Seuil | |
|---|---:|---:|:-:|
| Sharpe dégonflé (DSR) — probabilité que le vrai Sharpe > le meilleur hasard parmi les essais | 0.734 | ≥ 0.95 | ❌ |
| p-valeur exacte face au nul (mêmes scores permutés entre contrats par blocs d'une semaine) | 0.040 | ≤ 0.05 | ✅ |
| Probabilité de sur-ajustement du backtest (PBO, CSCV sur la grille) | 0.048 | ≤ 0.30 | ✅ |
| Sharpe annualisé net de coûts (quotidien) | 1.503 | ≥ 0.80 | ✅ |
| Part des années civiles positives (années de moins de 90 jours exclues) | 0.750 | ≥ 0.60 | ✅ |
| Mois hors échantillon | 37.105 | ≥ 12.00 | ✅ |
| Sharpe avec coûts doublés | 0.852 | ≥ 0.00 | ✅ |
| Sharpe avec une barre de latence en plus | 1.413 | ≥ 0.00 | ✅ |
| Sharpe avec stops exécutés au pire (plus bas / plus haut de la bougie) | -0.045 | ≥ 0.00 | ❌ |

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

Par modèle (horizon de détention) : `gbm_h16` IC 0.0452 (t 15.2), `ridge_h16` IC 0.0379 (t 13.8).

IC réalisé par année : 2023 : 0.0101, 2024 : 0.0195, 2025 : 0.0361, 2026 : 0.0146.

Modèle de direction du marché : corrélation 0.0398 (t ≈ 2.3), par année {2023: 0.0101, 2024: 0.0802, 2025: 0.0482, 2026: 0.0251} ; porte propre non franchie. Sharpe du livre avec exposition nette pilotée : 1.86 (utilisé en production : non).

## Performance nette du portefeuille (bêta-neutre, maker d'abord)

| Mesure | Valeur |
|---|---:|
| Sharpe annualisé (quotidien) | 1.50 |
| Intervalle bootstrap 90 % du Sharpe | [0.75 ; 2.65] |
| Rendement annualisé (CAGR) | 22.9 % |
| Volatilité annualisée | 12.1 % |
| Perte maximale (drawdown) | -9.8 % |
| Calmar | 2.33 |
| Pire / meilleur jour | -3.00 % / 9.57 % |
| Jours positifs | 53.4 % |
| PnL brut annuel (avant coûts) | 25.6 % |
| Frais / spread / impact annuels | 4.4 % / 1.7 % / 1.4 % |
| Funding annuel (+ = encaissé) | 3.3 % |
| Rotation annuelle (× capital) | 136 |
| Exposition brute / nette / bêta moyennes | 0.56 / 0.01 / 0.01 |
| Positions moyennes | 32.0 |
| Stops catastrophe déclenchés par an | 221 |

### Par année

| Année | Rendement | Sharpe | Drawdown max | P&L jambe acheteuse | P&L jambe vendeuse | Funding | Coûts | Rotation | Stops |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 2023 | -1.9 % | -1.42 | -3.9 % | 2.6 % | -5.3 % | 1.1 % | 0.2 % | 3 | 56 |
| 2024 | 19.4 % | 1.88 | -8.5 % | 0.1 % | 20.9 % | 1.9 % | 4.7 % | 90 | 149 |
| 2025 | 51.9 % | 2.46 | -9.8 % | -16.5 % | 70.1 % | 5.3 % | 15.6 % | 286 | 292 |
| 2026 | 6.2 % | 1.00 | -5.0 % | -4.5 % | 11.7 % | 1.9 % | 2.8 % | 42 | 186 |

## Tests statistiques

- **Probabilistic Sharpe Ratio** (vrai Sharpe > 0) : 1.000.
- **Deflated Sharpe Ratio** (37 essais effectifs comptés) : 0.734.
- **Nul par permutation** : Sharpe réel au percentile 100 %, p-valeur exacte 0.040 ; 95ᵉ percentile du nul 0.70 (24 répliques).
- **Test SPA de Hansen** (p-valeur, H0 : aucun avantage) : 0.007.
- **PBO** sur la grille : 0.048.
- **Historique minimal** pour conclure à 95 % : 270 jours.
- **Stress** : coûts ×2 → Sharpe 0.85 ; une barre de latence → Sharpe 1.41 ; stops exécutés au pire → Sharpe -0.04.
- **Concentration** : sans ses 5 meilleurs jours, Sharpe 1.28 et rendement annualisé 15.1 %.
- **Sans stops catastrophe** (diagnostic) : Sharpe 1.60, rendement annualisé 20.7 %, drawdown max -9.7 %.

### Économie du signal sans arrêt (diagnostic, hors porte)

Même stratégie sur toute la période, contrôles de drawdown et de perte journalière désactivés : ce que le signal rapporte et coûte réellement, année par année.

- Sharpe 1.48, CAGR 22.4 %, drawdown max -9.8 % ;
- P&L brut 25.2 %/an contre coûts 7.6 %/an, rotation 136×/an, exposition brute moyenne 0.56 ;
- par année : 2023 : -1.9 % ; 2024 : 19.4 % ; 2025 : 51.9 % ; 2026 : 5.1 %.

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
