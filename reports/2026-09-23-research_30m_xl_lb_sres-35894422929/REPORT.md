# Rapport de recherche Hermes

*Généré le 2026-09-23T18:33:54+00:00 · configuration `f2227b88c6bd` · 4622 s de calcul.*

## Verdict : ⛔ NON PROMU — interdit de capital réel

Toutes les mesures ci-dessous sont **hors échantillon** (walk-forward : chaque prédiction vient d'un modèle entraîné uniquement sur le passé) et **nettes de frais, spread, impact et funding**.

| Porte | Valeur | Seuil | |
|---|---:|---:|:-:|
| Sharpe dégonflé (DSR) — probabilité que le vrai Sharpe > le meilleur hasard parmi les essais | 0.459 | ≥ 0.95 | ❌ |
| p-valeur exacte face au nul (mêmes scores permutés entre contrats par blocs d'une semaine) | 0.040 | ≤ 0.05 | ✅ |
| Probabilité de sur-ajustement du backtest (PBO, CSCV sur la grille) | 0.385 | ≤ 0.30 | ❌ |
| Sharpe annualisé net de coûts (quotidien) | 1.096 | ≥ 0.80 | ✅ |
| Part des années civiles positives (années de moins de 90 jours exclues) | 0.750 | ≥ 0.60 | ✅ |
| Mois hors échantillon | 37.105 | ≥ 12.00 | ✅ |
| Sharpe avec coûts doublés | 0.643 | ≥ 0.00 | ✅ |
| Sharpe avec une barre de latence en plus | 1.082 | ≥ 0.00 | ✅ |
| Sharpe avec stops exécutés au pire (plus bas / plus haut de la bougie) | 0.394 | ≥ 0.00 | ✅ |

## Données

- Source : `binance_archive`, barres `30m`, du 2022-06-01 au 2026-08-31.
- 199 contrats ayant figuré dans l'univers point-in-time (≈ 29 membres en moyenne), 2 149 680 échantillons × 122 variables.
- Hors échantillon à partir du 2023-07-31.

## Qualité de prédiction (IC transversal de Spearman, cible résiduelle nette du funding)

| Horizon | IC moyen | t (Newey-West) | IC/σ | % périodes > 0 |
|---|---:|---:|---:|---:|
| 16 barres | 0.0440 | 12.42 | 0.221 | 58.7 % |
| 48 barres | 0.0452 | 8.32 | 0.225 | 58.9 % |
| 96 barres | 0.0392 | 5.77 | 0.195 | 57.0 % |

Par modèle (horizon de détention) : `gbm_h48` IC 0.0413 (t 7.8), `ridge_h48` IC 0.0342 (t 6.6).

IC réalisé par année : 2023 : 0.0135, 2024 : 0.0218, 2025 : 0.0441, 2026 : -0.0221.

Modèle de direction du marché : corrélation 0.0451 (t ≈ 1.5), par année {2023: 0.0049, 2024: 0.113, 2025: 0.0486, 2026: 0.0227} ; porte propre non franchie. Sharpe du livre avec exposition nette pilotée : 1.40 (utilisé en production : non).

## Performance nette du portefeuille (bêta-neutre, maker d'abord)

| Mesure | Valeur |
|---|---:|
| Sharpe annualisé (quotidien) | 1.10 |
| Intervalle bootstrap 90 % du Sharpe | [0.20 ; 2.07] |
| Rendement annualisé (CAGR) | 12.6 % |
| Volatilité annualisée | 10.5 % |
| Perte maximale (drawdown) | -11.0 % |
| Calmar | 1.14 |
| Pire / meilleur jour | -3.41 % / 3.04 % |
| Jours positifs | 43.0 % |
| PnL brut annuel (avant coûts) | 15.6 % |
| Frais / spread / impact annuels | 3.8 % / 1.5 % / 0.7 % |
| Funding annuel (+ = encaissé) | 2.7 % |
| Rotation annuelle (× capital) | 117 |
| Exposition brute / nette / bêta moyennes | 0.48 / 0.02 / 0.01 |
| Positions moyennes | 20.8 |
| Stops catastrophe déclenchés par an | 43 |

### Par année

| Année | Rendement | Sharpe | Drawdown max | P&L jambe acheteuse | P&L jambe vendeuse | Funding | Coûts | Rotation | Stops |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 2023 | 0.6 % | 0.24 | -7.6 % | 10.3 % | -11.7 % | 2.5 % | 0.4 % | 8 | 22 |
| 2024 | 12.4 % | 1.21 | -11.0 % | 5.1 % | 10.2 % | 1.6 % | 4.6 % | 91 | 38 |
| 2025 | 36.7 % | 2.19 | -6.4 % | 4.2 % | 38.6 % | 2.8 % | 13.2 % | 258 | 52 |
| 2026 | -6.8 % | -2.71 | -7.2 % | -5.6 % | -2.6 % | 1.5 % | 0.3 % | 5 | 22 |

## Tests statistiques

- **Probabilistic Sharpe Ratio** (vrai Sharpe > 0) : 0.978.
- **Deflated Sharpe Ratio** (33 essais effectifs comptés) : 0.459.
- **Nul par permutation** : Sharpe réel au percentile 100 %, p-valeur exacte 0.040 ; 95ᵉ percentile du nul 0.28 (24 répliques).
- **Test SPA de Hansen** (p-valeur, H0 : aucun avantage) : 0.033.
- **PBO** sur la grille : 0.385.
- **Historique minimal** pour conclure à 95 % : 750 jours.
- **Stress** : coûts ×2 → Sharpe 0.64 ; une barre de latence → Sharpe 1.08 ; stops exécutés au pire → Sharpe 0.39.
- **Concentration** : sans ses 5 meilleurs jours, Sharpe 0.77 et rendement annualisé 7.7 %.
- **Sans stops catastrophe** (diagnostic) : Sharpe 1.00, rendement annualisé 11.2 %, drawdown max -10.8 %.

### Économie du signal sans arrêt (diagnostic, hors porte)

Même stratégie sur toute la période, contrôles de drawdown et de perte journalière désactivés : ce que le signal rapporte et coûte réellement, année par année.

- Sharpe 1.12, CAGR 13.0 %, drawdown max -12.1 % ;
- P&L brut 16.0 %/an contre coûts 5.9 %/an, rotation 116×/an, exposition brute moyenne 0.50 ;
- par année : 2023 : 0.6 % ; 2024 : 12.9 % ; 2025 : 37.0 % ; 2026 : -6.2 %.

### Grille de construction (base du PBO)

| Configuration | Sharpe | CAGR | Drawdown | Rotation |
|---|---:|---:|---:|---:|
| `{'holding_horizon': 16, 'cost_aversion': 0.5}` | 1.58 | 22.6 % | -14.1 % | 348 |
| `{'holding_horizon': 16, 'cost_aversion': 1.0}` | 1.62 | 21.7 % | -12.9 % | 228 |
| `{'holding_horizon': 16, 'cost_aversion': 2.0}` | 1.31 | 15.7 % | -13.5 % | 104 |
| `{'holding_horizon': 48, 'cost_aversion': 0.5}` | 1.35 | 17.6 % | -13.5 % | 233 |
| `{'holding_horizon': 48, 'cost_aversion': 1.0}` | 1.32 | 17.2 % | -12.7 % | 179 |
| `{'holding_horizon': 48, 'cost_aversion': 2.0}` | 1.10 | 12.6 % | -11.0 % | 117 |
| `{'holding_horizon': 96, 'cost_aversion': 0.5}` | 0.99 | 12.0 % | -15.1 % | 176 |
| `{'holding_horizon': 96, 'cost_aversion': 1.0}` | 0.87 | 9.8 % | -13.9 % | 156 |
| `{'holding_horizon': 96, 'cost_aversion': 2.0}` | 0.62 | 6.2 % | -12.2 % | 108 |

## Variables les plus utilisées (gain LightGBM, moyenne des plis)

`log_dollar_volume` 77.6 %, `amihud_long` 73.0 %, `funding_sum_43200m` 70.3 %, `cs_amihud_long` 64.7 %, `volvol_long` 55.8 %, `beta` 53.6 %, `funding_sum_10080m` 49.8 %, `kurt_long` 49.6 %, `cs_funding_sum_43200m` 48.6 %, `skew_long` 48.0 %, `ret_43200m` 45.1 %, `iret_43200m` 43.0 %, `cs_beta` 42.1 %, `cs_funding_sum_10080m` 42.0 %, `trade_size` 40.7 %

## Lecture honnête

Un backtest, même hors échantillon, reste une estimation : l'intervalle de confiance du Sharpe ci-dessus dit à quel point. La porte de promotion est volontairement sévère ; un résultat qui ne la franchit pas ne trade pas en réel, quel que soit l'attrait des chiffres. Un résultat qui la franchit démarre en papier, puis en réel à capital réduit, et reste surveillé par l'IC réalisé en continu.
