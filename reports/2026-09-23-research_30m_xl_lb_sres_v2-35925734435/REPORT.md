# Rapport de recherche Hermes

*Généré le 2026-09-23T23:25:51+00:00 · configuration `4e65cd957f75` · 5139 s de calcul.*

## Verdict : ⛔ NON PROMU — interdit de capital réel

Toutes les mesures ci-dessous sont **hors échantillon** (walk-forward : chaque prédiction vient d'un modèle entraîné uniquement sur le passé) et **nettes de frais, spread, impact et funding**.

| Porte | Valeur | Seuil | |
|---|---:|---:|:-:|
| Sharpe dégonflé (DSR) — probabilité que le vrai Sharpe > le meilleur hasard parmi les essais | 0.245 | ≥ 0.95 | ❌ |
| p-valeur exacte face au nul (mêmes scores permutés entre contrats par blocs d'une semaine) | 0.040 | ≤ 0.05 | ✅ |
| Probabilité de sur-ajustement du backtest (PBO, CSCV sur la grille) | 0.103 | ≤ 0.30 | ✅ |
| Sharpe annualisé net de coûts (quotidien) | 0.825 | ≥ 0.80 | ✅ |
| Part des années civiles positives (années de moins de 90 jours exclues) | 0.750 | ≥ 0.60 | ✅ |
| Mois hors échantillon | 37.105 | ≥ 12.00 | ✅ |
| Sharpe avec coûts doublés | -0.006 | ≥ 0.00 | ❌ |
| Sharpe avec une barre de latence en plus | 0.745 | ≥ 0.00 | ✅ |
| Sharpe avec stops exécutés au pire (plus bas / plus haut de la bougie) | 0.189 | ≥ 0.00 | ✅ |

## Données

- Source : `binance_archive`, barres `30m`, du 2022-06-01 au 2026-08-31.
- 198 contrats ayant figuré dans l'univers point-in-time (≈ 29 membres en moyenne), 2 149 680 échantillons × 123 variables.
- Hors échantillon à partir du 2023-07-31.

## Qualité de prédiction (IC transversal de Spearman, cible résiduelle nette du funding)

| Horizon | IC moyen | t (Newey-West) | IC/σ | % périodes > 0 |
|---|---:|---:|---:|---:|
| 16 barres | 0.0465 | 13.28 | 0.232 | 59.3 % |
| 48 barres | 0.0472 | 9.07 | 0.236 | 59.8 % |
| 96 barres | 0.0409 | 6.41 | 0.206 | 58.0 % |

Par modèle (horizon de détention) : `gbm_h48` IC 0.0468 (t 9.2), `ridge_h48` IC 0.0339 (t 6.5).

IC réalisé par année : 2023 : 0.0123, 2024 : 0.0258, 2025 : 0.0435, 2026 : -0.0228.

Modèle de direction du marché : corrélation 0.0451 (t ≈ 1.5), par année {2023: 0.0049, 2024: 0.113, 2025: 0.0486, 2026: 0.0227} ; porte propre non franchie. Sharpe du livre avec exposition nette pilotée : 1.10 (utilisé en production : non).

## Performance nette du portefeuille (bêta-neutre, maker d'abord)

| Mesure | Valeur |
|---|---:|
| Sharpe annualisé (quotidien) | 0.82 |
| Intervalle bootstrap 90 % du Sharpe | [-0.13 ; 1.78] |
| Rendement annualisé (CAGR) | 8.4 % |
| Volatilité annualisée | 10.2 % |
| Perte maximale (drawdown) | -12.3 % |
| Calmar | 0.69 |
| Pire / meilleur jour | -2.80 % / 3.69 % |
| Jours positifs | 41.9 % |
| PnL brut annuel (avant coûts) | 12.0 % |
| Frais / spread / impact annuels | 3.9 % / 1.7 % / 0.7 % |
| Funding annuel (+ = encaissé) | 2.9 % |
| Rotation annuelle (× capital) | 122 |
| Exposition brute / nette / bêta moyennes | 0.43 / 0.02 / 0.01 |
| Positions moyennes | 18.8 |
| Stops catastrophe déclenchés par an | 38 |

### Par année

| Année | Rendement | Sharpe | Drawdown max | P&L jambe acheteuse | P&L jambe vendeuse | Funding | Coûts | Rotation | Stops |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 2023 | 1.3 % | 0.62 | -4.8 % | 9.5 % | -9.9 % | 2.0 % | 0.3 % | 6 | 25 |
| 2024 | 9.9 % | 0.99 | -12.3 % | 7.7 % | 6.8 % | 1.2 % | 5.8 % | 113 | 23 |
| 2025 | 25.9 % | 1.72 | -7.1 % | -11.8 % | 45.3 % | 3.3 % | 12.9 % | 252 | 46 |
| 2026 | -8.3 % | -2.65 | -9.2 % | -7.5 % | -3.2 % | 2.4 % | 0.3 % | 6 | 23 |

## Tests statistiques

- **Probabilistic Sharpe Ratio** (vrai Sharpe > 0) : 0.932.
- **Deflated Sharpe Ratio** (38 essais effectifs comptés) : 0.245.
- **Nul par permutation** : Sharpe réel au percentile 100 %, p-valeur exacte 0.040 ; 95ᵉ percentile du nul 0.43 (24 répliques).
- **Test SPA de Hansen** (p-valeur, H0 : aucun avantage) : 0.079.
- **PBO** sur la grille : 0.103.
- **Historique minimal** pour conclure à 95 % : 1373 jours.
- **Stress** : coûts ×2 → Sharpe -0.01 ; une barre de latence → Sharpe 0.75 ; stops exécutés au pire → Sharpe 0.19.
- **Concentration** : sans ses 5 meilleurs jours, Sharpe 0.44 et rendement annualisé 3.8 %.
- **Sans stops catastrophe** (diagnostic) : Sharpe 0.73, rendement annualisé 7.4 %, drawdown max -11.9 %.

### Économie du signal sans arrêt (diagnostic, hors porte)

Même stratégie sur toute la période, contrôles de drawdown et de perte journalière désactivés : ce que le signal rapporte et coûte réellement, année par année.

- Sharpe 0.76, CAGR 7.9 %, drawdown max -16.1 % ;
- P&L brut 11.1 %/an contre coûts 5.9 %/an, rotation 114×/an, exposition brute moyenne 0.49 ;
- par année : 2023 : 1.3 % ; 2024 : 7.4 % ; 2025 : 26.4 % ; 2026 : -7.8 %.

### Grille de construction (base du PBO)

| Configuration | Sharpe | CAGR | Drawdown | Rotation |
|---|---:|---:|---:|---:|
| `{'holding_horizon': 16, 'cost_aversion': 0.5}` | 1.55 | 22.3 % | -15.0 % | 372 |
| `{'holding_horizon': 16, 'cost_aversion': 1.0}` | 1.62 | 21.9 % | -13.7 % | 249 |
| `{'holding_horizon': 16, 'cost_aversion': 2.0}` | 1.25 | 15.2 % | -13.0 % | 113 |
| `{'holding_horizon': 48, 'cost_aversion': 0.5}` | 1.13 | 12.6 % | -16.4 % | 241 |
| `{'holding_horizon': 48, 'cost_aversion': 1.0}` | 1.06 | 11.6 % | -15.1 % | 201 |
| `{'holding_horizon': 48, 'cost_aversion': 2.0}` | 0.82 | 8.4 % | -12.3 % | 122 |
| `{'holding_horizon': 96, 'cost_aversion': 0.5}` | 0.83 | 7.8 % | -18.7 % | 164 |
| `{'holding_horizon': 96, 'cost_aversion': 1.0}` | 0.65 | 5.9 % | -18.2 % | 144 |
| `{'holding_horizon': 96, 'cost_aversion': 2.0}` | 0.43 | 3.6 % | -16.8 % | 122 |

## Variables les plus utilisées (gain LightGBM, moyenne des plis)

`funding_sum_43200m` 77.5 %, `log_dollar_volume` 65.7 %, `amihud_long` 64.3 %, `volvol_long` 56.7 %, `cs_funding_sum_43200m` 56.5 %, `beta` 54.6 %, `kurt_long` 54.2 %, `cs_amihud_long` 53.7 %, `funding_sum_10080m` 48.7 %, `skew_long` 47.7 %, `cs_funding_sum_10080m` 45.9 %, `trade_size` 43.2 %, `iret_43200m` 42.8 %, `ret_43200m` 40.1 %, `cs_beta` 39.6 %

## Lecture honnête

Un backtest, même hors échantillon, reste une estimation : l'intervalle de confiance du Sharpe ci-dessus dit à quel point. La porte de promotion est volontairement sévère ; un résultat qui ne la franchit pas ne trade pas en réel, quel que soit l'attrait des chiffres. Un résultat qui la franchit démarre en papier, puis en réel à capital réduit, et reste surveillé par l'IC réalisé en continu.
