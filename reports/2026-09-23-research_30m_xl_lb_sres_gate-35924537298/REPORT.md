# Rapport de recherche Hermes

*Généré le 2026-09-23T22:59:23+00:00 · configuration `3268a7812bfa` · 4300 s de calcul.*

## Verdict : ⛔ NON PROMU — interdit de capital réel

Toutes les mesures ci-dessous sont **hors échantillon** (walk-forward : chaque prédiction vient d'un modèle entraîné uniquement sur le passé) et **nettes de frais, spread, impact et funding**.

| Porte | Valeur | Seuil | |
|---|---:|---:|:-:|
| Sharpe dégonflé (DSR) — probabilité que le vrai Sharpe > le meilleur hasard parmi les essais | 0.379 | ≥ 0.95 | ❌ |
| p-valeur exacte face au nul (mêmes scores permutés entre contrats par blocs d'une semaine) | 0.040 | ≤ 0.05 | ✅ |
| Probabilité de sur-ajustement du backtest (PBO, CSCV sur la grille) | 0.258 | ≤ 0.30 | ✅ |
| Sharpe annualisé net de coûts (quotidien) | 1.005 | ≥ 0.80 | ✅ |
| Part des années civiles positives (années de moins de 90 jours exclues) | 0.500 | ≥ 0.60 | ❌ |
| Mois hors échantillon | 37.105 | ≥ 12.00 | ✅ |
| Sharpe avec coûts doublés | 0.498 | ≥ 0.00 | ✅ |
| Sharpe avec une barre de latence en plus | 0.981 | ≥ 0.00 | ✅ |
| Sharpe avec stops exécutés au pire (plus bas / plus haut de la bougie) | 0.280 | ≥ 0.00 | ✅ |

## Données

- Source : `binance_archive`, barres `30m`, du 2022-06-01 au 2026-08-31.
- 198 contrats ayant figuré dans l'univers point-in-time (≈ 29 membres en moyenne), 2 149 680 échantillons × 122 variables.
- Hors échantillon à partir du 2023-07-31.

## Qualité de prédiction (IC transversal de Spearman, cible résiduelle nette du funding)

| Horizon | IC moyen | t (Newey-West) | IC/σ | % périodes > 0 |
|---|---:|---:|---:|---:|
| 16 barres | 0.0440 | 12.42 | 0.221 | 58.7 % |
| 48 barres | 0.0452 | 8.32 | 0.225 | 58.9 % |
| 96 barres | 0.0392 | 5.77 | 0.195 | 57.0 % |

Par modèle (horizon de détention) : `gbm_h48` IC 0.0413 (t 7.8), `ridge_h48` IC 0.0342 (t 6.6).

IC réalisé par année : 2023 : 0.0135, 2024 : 0.0218, 2025 : 0.0441, 2026 : -0.0221.

Modèle de direction du marché : corrélation 0.0451 (t ≈ 1.5), par année {2023: 0.0049, 2024: 0.113, 2025: 0.0486, 2026: 0.0227} ; porte propre non franchie. Sharpe du livre avec exposition nette pilotée : 1.31 (utilisé en production : non).

## Performance nette du portefeuille (bêta-neutre, maker d'abord)

| Mesure | Valeur |
|---|---:|
| Sharpe annualisé (quotidien) | 1.00 |
| Intervalle bootstrap 90 % du Sharpe | [0.11 ; 1.96] |
| Rendement annualisé (CAGR) | 10.6 % |
| Volatilité annualisée | 9.7 % |
| Perte maximale (drawdown) | -10.5 % |
| Calmar | 1.01 |
| Pire / meilleur jour | -2.32 % / 3.08 % |
| Jours positifs | 50.5 % |
| PnL brut annuel (avant coûts) | 13.7 % |
| Frais / spread / impact annuels | 3.1 % / 1.3 % / 0.6 % |
| Funding annuel (+ = encaissé) | 1.9 % |
| Rotation annuelle (× capital) | 97 |
| Exposition brute / nette / bêta moyennes | 0.46 / 0.02 / 0.01 |
| Positions moyennes | 22.5 |
| Stops catastrophe déclenchés par an | 45 |

### Par année

| Année | Rendement | Sharpe | Drawdown max | P&L jambe acheteuse | P&L jambe vendeuse | Funding | Coûts | Rotation | Stops |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 2023 | -1.5 % | -0.58 | -7.0 % | 8.0 % | -10.7 % | 1.6 % | 0.3 % | 7 | 21 |
| 2024 | 15.2 % | 1.47 | -10.5 % | 1.4 % | 16.1 % | 1.6 % | 4.2 % | 84 | 47 |
| 2025 | 24.4 % | 1.71 | -6.4 % | 3.0 % | 28.2 % | 2.1 % | 10.8 % | 209 | 52 |
| 2026 | -3.2 % | -2.29 | -3.8 % | -2.5 % | -1.3 % | 0.6 % | 0.1 % | 1 | 18 |

## Tests statistiques

- **Probabilistic Sharpe Ratio** (vrai Sharpe > 0) : 0.968.
- **Deflated Sharpe Ratio** (35 essais effectifs comptés) : 0.379.
- **Nul par permutation** : Sharpe réel au percentile 100 %, p-valeur exacte 0.040 ; 95ᵉ percentile du nul 0.43 (24 répliques).
- **Test SPA de Hansen** (p-valeur, H0 : aucun avantage) : 0.044.
- **PBO** sur la grille : 0.258.
- **Historique minimal** pour conclure à 95 % : 885 jours.
- **Stress** : coûts ×2 → Sharpe 0.50 ; une barre de latence → Sharpe 0.98 ; stops exécutés au pire → Sharpe 0.28.
- **Concentration** : sans ses 5 meilleurs jours, Sharpe 0.64 et rendement annualisé 5.8 %.
- **Sans stops catastrophe** (diagnostic) : Sharpe 0.91, rendement annualisé 9.5 %, drawdown max -10.3 %.

### Économie du signal sans arrêt (diagnostic, hors porte)

Même stratégie sur toute la période, contrôles de drawdown et de perte journalière désactivés : ce que le signal rapporte et coûte réellement, année par année.

- Sharpe 1.01, CAGR 10.7 %, drawdown max -10.6 % ;
- P&L brut 13.8 %/an contre coûts 5.0 %/an, rotation 97×/an, exposition brute moyenne 0.46 ;
- par année : 2023 : -1.5 % ; 2024 : 15.3 % ; 2025 : 24.3 % ; 2026 : -3.0 %.

### Grille de construction (base du PBO)

| Configuration | Sharpe | CAGR | Drawdown | Rotation |
|---|---:|---:|---:|---:|
| `{'holding_horizon': 16, 'cost_aversion': 0.5}` | 1.70 | 20.9 % | -10.4 % | 267 |
| `{'holding_horizon': 16, 'cost_aversion': 1.0}` | 1.59 | 18.1 % | -9.2 % | 171 |
| `{'holding_horizon': 16, 'cost_aversion': 2.0}` | 1.29 | 13.8 % | -9.4 % | 77 |
| `{'holding_horizon': 48, 'cost_aversion': 0.5}` | 1.29 | 15.6 % | -13.2 % | 201 |
| `{'holding_horizon': 48, 'cost_aversion': 1.0}` | 1.27 | 15.4 % | -11.7 % | 146 |
| `{'holding_horizon': 48, 'cost_aversion': 2.0}` | 1.00 | 10.6 % | -10.5 % | 97 |
| `{'holding_horizon': 96, 'cost_aversion': 0.5}` | 0.99 | 11.4 % | -15.1 % | 160 |
| `{'holding_horizon': 96, 'cost_aversion': 1.0}` | 0.90 | 9.7 % | -13.1 % | 139 |
| `{'holding_horizon': 96, 'cost_aversion': 2.0}` | 0.70 | 6.7 % | -11.5 % | 93 |

## Variables les plus utilisées (gain LightGBM, moyenne des plis)

`log_dollar_volume` 77.6 %, `amihud_long` 73.0 %, `funding_sum_43200m` 70.3 %, `cs_amihud_long` 64.7 %, `volvol_long` 55.8 %, `beta` 53.6 %, `funding_sum_10080m` 49.8 %, `kurt_long` 49.6 %, `cs_funding_sum_43200m` 48.6 %, `skew_long` 48.0 %, `ret_43200m` 45.1 %, `iret_43200m` 43.0 %, `cs_beta` 42.1 %, `cs_funding_sum_10080m` 42.0 %, `trade_size` 40.7 %

## Lecture honnête

Un backtest, même hors échantillon, reste une estimation : l'intervalle de confiance du Sharpe ci-dessus dit à quel point. La porte de promotion est volontairement sévère ; un résultat qui ne la franchit pas ne trade pas en réel, quel que soit l'attrait des chiffres. Un résultat qui la franchit démarre en papier, puis en réel à capital réduit, et reste surveillé par l'IC réalisé en continu.
