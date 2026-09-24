# Rapport de recherche Hermes

*Généré le 2026-09-24T05:39:01+00:00 · configuration `1b8c803a29a2` · 8709 s de calcul.*

## Verdict : ⛔ NON PROMU — interdit de capital réel

Toutes les mesures ci-dessous sont **hors échantillon** (walk-forward : chaque prédiction vient d'un modèle entraîné uniquement sur le passé) et **nettes de frais, spread, impact et funding**.

| Porte | Valeur | Seuil | |
|---|---:|---:|:-:|
| Sharpe dégonflé (DSR) — probabilité que le vrai Sharpe > le meilleur hasard parmi les essais | 0.639 | ≥ 0.95 | ❌ |
| p-valeur exacte face au nul (mêmes scores permutés entre contrats par blocs d'une semaine) | 0.040 | ≤ 0.05 | ✅ |
| Probabilité de sur-ajustement du backtest (PBO, CSCV sur la grille) | 0.353 | ≤ 0.30 | ❌ |
| Sharpe annualisé net de coûts (quotidien) | 1.363 | ≥ 0.80 | ✅ |
| Part des années civiles positives (années de moins de 90 jours exclues) | 0.750 | ≥ 0.60 | ✅ |
| Mois hors échantillon | 37.105 | ≥ 12.00 | ✅ |
| Sharpe avec coûts doublés | 0.347 | ≥ 0.00 | ✅ |
| Sharpe avec une barre de latence en plus | 1.269 | ≥ 0.00 | ✅ |
| Sharpe avec stops exécutés au pire (plus bas / plus haut de la bougie) | 0.452 | ≥ 0.00 | ✅ |

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

Modèle de direction du marché : corrélation 0.0451 (t ≈ 1.5), par année {2023: 0.0049, 2024: 0.113, 2025: 0.0486, 2026: 0.0227} ; porte propre non franchie. Sharpe du livre avec exposition nette pilotée : 1.48 (utilisé en production : non).

## Performance nette du portefeuille (bêta-neutre, maker d'abord)

| Mesure | Valeur |
|---|---:|
| Sharpe annualisé (quotidien) | 1.36 |
| Intervalle bootstrap 90 % du Sharpe | [0.49 ; 2.45] |
| Rendement annualisé (CAGR) | 16.4 % |
| Volatilité annualisée | 10.4 % |
| Perte maximale (drawdown) | -11.7 % |
| Calmar | 1.40 |
| Pire / meilleur jour | -2.89 % / 3.12 % |
| Jours positifs | 50.6 % |
| PnL brut annuel (avant coûts) | 20.9 % |
| Frais / spread / impact annuels | 5.1 % / 2.1 % / 0.8 % |
| Funding annuel (+ = encaissé) | 2.8 % |
| Rotation annuelle (× capital) | 159 |
| Exposition brute / nette / bêta moyennes | 0.51 / 0.02 / 0.01 |
| Positions moyennes | 26.3 |
| Stops catastrophe déclenchés par an | 54 |

### Par année

| Année | Rendement | Sharpe | Drawdown max | P&L jambe acheteuse | P&L jambe vendeuse | Funding | Coûts | Rotation | Stops |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 2023 | 0.8 % | 0.37 | -5.2 % | 5.8 % | -6.5 % | 2.1 % | 0.5 % | 12 | 29 |
| 2024 | 13.9 % | 1.40 | -11.7 % | 2.5 % | 15.4 % | 1.6 % | 6.0 % | 120 | 42 |
| 2025 | 50.2 % | 2.83 | -7.5 % | 6.1 % | 49.3 % | 3.1 % | 16.9 % | 337 | 51 |
| 2026 | -7.4 % | -2.62 | -7.6 % | -3.8 % | -4.5 % | 1.8 % | 1.1 % | 22 | 44 |

## Tests statistiques

- **Probabilistic Sharpe Ratio** (vrai Sharpe > 0) : 0.995.
- **Deflated Sharpe Ratio** (44 essais effectifs comptés) : 0.639.
- **Nul par permutation** : Sharpe réel au percentile 100 %, p-valeur exacte 0.040 ; 95ᵉ percentile du nul 0.25 (24 répliques).
- **Test SPA de Hansen** (p-valeur, H0 : aucun avantage) : 0.011.
- **PBO** sur la grille : 0.353.
- **Historique minimal** pour conclure à 95 % : 453 jours.
- **Stress** : coûts ×2 → Sharpe 0.35 ; une barre de latence → Sharpe 1.27 ; stops exécutés au pire → Sharpe 0.45.
- **Concentration** : sans ses 5 meilleurs jours, Sharpe 1.10 et rendement annualisé 11.4 %.
- **Sans stops catastrophe** (diagnostic) : Sharpe 1.32, rendement annualisé 15.5 %, drawdown max -11.0 %.

### Économie du signal sans arrêt (diagnostic, hors porte)

Même stratégie sur toute la période, contrôles de drawdown et de perte journalière désactivés : ce que le signal rapporte et coûte réellement, année par année.

- Sharpe 1.36, CAGR 16.4 %, drawdown max -12.9 % ;
- P&L brut 20.7 %/an contre coûts 7.7 %/an, rotation 155×/an, exposition brute moyenne 0.52 ;
- par année : 2023 : 0.8 % ; 2024 : 14.0 % ; 2025 : 50.2 % ; 2026 : -7.3 %.

### Grille de construction (base du PBO)

| Configuration | Sharpe | CAGR | Drawdown | Rotation |
|---|---:|---:|---:|---:|
| `{'holding_horizon': 16, 'cost_aversion': 0.5}` | 1.58 | 22.6 % | -14.1 % | 348 |
| `{'holding_horizon': 16, 'cost_aversion': 1.0}` | 1.62 | 21.7 % | -12.9 % | 228 |
| `{'holding_horizon': 16, 'cost_aversion': 2.0}` | 1.31 | 15.7 % | -13.5 % | 104 |
| `{'holding_horizon': 48, 'cost_aversion': 0.5}` | 1.35 | 17.6 % | -13.4 % | 232 |
| `{'holding_horizon': 48, 'cost_aversion': 1.0}` | 1.31 | 17.0 % | -12.7 % | 180 |
| `{'holding_horizon': 48, 'cost_aversion': 2.0}` | 1.10 | 12.6 % | -11.0 % | 117 |
| `{'holding_horizon': 96, 'cost_aversion': 0.5}` | 0.99 | 12.0 % | -15.1 % | 176 |
| `{'holding_horizon': 96, 'cost_aversion': 1.0}` | 0.87 | 9.8 % | -13.9 % | 156 |
| `{'holding_horizon': 96, 'cost_aversion': 2.0}` | 0.62 | 6.2 % | -12.2 % | 108 |

### Sélection du réglage en walk-forward (diagnostic, hors porte)

Chaque mois, le réglage de la grille au meilleur Sharpe sur les 365 jours précédents (passé seulement ; réglage configuré tant que 180 jours d'historique manquent), 0.1 % du capital payé à chaque changement :

- Sharpe 1.20, CAGR 15.4 %, drawdown max -13.7 %, 8 changements ;
- par année : 2023 : 0.8 % ; 2024 : 9.5 % ; 2025 : 56.1 % ; 2026 : -9.7 % ;
- mois par réglage : `{'holding_horizon': 16, 'cost_aversion': 0.5}` 37 %, `{'holding_horizon': 16, 'cost_aversion': 1.0}` 24 %, `default` 18 %, `{'holding_horizon': 16, 'cost_aversion': 2.0}` 10 %, `{'holding_horizon': 48, 'cost_aversion': 0.5}` 8 %, `{'holding_horizon': 48, 'cost_aversion': 1.0}` 3 %.

## Variables les plus utilisées (gain LightGBM, moyenne des plis)

`log_dollar_volume` 77.6 %, `amihud_long` 73.0 %, `funding_sum_43200m` 70.3 %, `cs_amihud_long` 64.7 %, `volvol_long` 55.8 %, `beta` 53.6 %, `funding_sum_10080m` 49.8 %, `kurt_long` 49.6 %, `cs_funding_sum_43200m` 48.6 %, `skew_long` 48.0 %, `ret_43200m` 45.1 %, `iret_43200m` 43.0 %, `cs_beta` 42.1 %, `cs_funding_sum_10080m` 42.0 %, `trade_size` 40.7 %

## Lecture honnête

Un backtest, même hors échantillon, reste une estimation : l'intervalle de confiance du Sharpe ci-dessus dit à quel point. La porte de promotion est volontairement sévère ; un résultat qui ne la franchit pas ne trade pas en réel, quel que soit l'attrait des chiffres. Un résultat qui la franchit démarre en papier, puis en réel à capital réduit, et reste surveillé par l'IC réalisé en continu.
