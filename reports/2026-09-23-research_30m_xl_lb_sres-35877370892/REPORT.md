# Rapport de recherche Hermes

*Généré le 2026-09-23T15:58:44+00:00 · configuration `171db5ca8eca` · 3936 s de calcul.*

## Verdict : ⛔ NON PROMU — interdit de capital réel

Toutes les mesures ci-dessous sont **hors échantillon** (walk-forward : chaque prédiction vient d'un modèle entraîné uniquement sur le passé) et **nettes de frais, spread, impact et funding**.

| Porte | Valeur | Seuil | |
|---|---:|---:|:-:|
| Sharpe dégonflé (DSR) — probabilité que le vrai Sharpe > le meilleur hasard parmi les essais | 0.312 | ≥ 0.95 | ❌ |
| p-valeur exacte face au nul (mêmes scores permutés entre contrats par blocs d'une semaine) | 0.040 | ≤ 0.05 | ✅ |
| Probabilité de sur-ajustement du backtest (PBO, CSCV sur la grille) | 0.452 | ≤ 0.30 | ❌ |
| Sharpe annualisé net de coûts (quotidien) | 0.837 | ≥ 0.80 | ✅ |
| Part des années civiles positives (années de moins de 90 jours exclues) | 0.750 | ≥ 0.60 | ✅ |
| Mois hors échantillon | 37.105 | ≥ 12.00 | ✅ |
| Sharpe avec coûts doublés | 0.204 | ≥ 0.00 | ✅ |
| Sharpe avec une barre de latence en plus | 0.564 | ≥ 0.00 | ✅ |
| Sharpe avec stops exécutés au pire (plus bas / plus haut de la bougie) | -0.041 | ≥ 0.00 | ❌ |

## Données

- Source : `binance_archive`, barres `30m`, du 2022-06-01 au 2026-08-31.
- 289 contrats ayant figuré dans l'univers point-in-time (≈ 29 membres en moyenne), 2 149 643 échantillons × 122 variables.
- Hors échantillon à partir du 2023-07-31.

## Qualité de prédiction (IC transversal de Spearman, cible résiduelle nette du funding)

| Horizon | IC moyen | t (Newey-West) | IC/σ | % périodes > 0 |
|---|---:|---:|---:|---:|
| 16 barres | 0.0419 | 12.54 | 0.210 | 58.6 % |
| 48 barres | 0.0450 | 8.68 | 0.223 | 59.0 % |
| 96 barres | 0.0426 | 6.24 | 0.213 | 58.3 % |

Par modèle (horizon de détention) : `gbm_h48` IC 0.0441 (t 8.6), `ridge_h48` IC 0.0311 (t 6.0).

IC réalisé par année : 2023 : 0.0090, 2024 : 0.0249, 2025 : 0.0347, 2026 : -0.0054.

Modèle de direction du marché : corrélation 0.0515 (t ≈ 1.7), par année {2023: -0.0236, 2024: 0.1126, 2025: 0.0801, 2026: 0.0191} ; porte propre non franchie. Sharpe du livre avec exposition nette pilotée : 1.10 (utilisé en production : non).

## Performance nette du portefeuille (bêta-neutre, maker d'abord)

| Mesure | Valeur |
|---|---:|
| Sharpe annualisé (quotidien) | 0.84 |
| Intervalle bootstrap 90 % du Sharpe | [-0.04 ; 1.80] |
| Rendement annualisé (CAGR) | 10.4 % |
| Volatilité annualisée | 10.2 % |
| Perte maximale (drawdown) | -12.7 % |
| Calmar | 0.82 |
| Pire / meilleur jour | -2.53 % / 9.54 % |
| Jours positifs | 45.9 % |
| PnL brut annuel (avant coûts) | 10.6 % |
| Frais / spread / impact annuels | 3.1 % / 1.3 % / 0.6 % |
| Funding annuel (+ = encaissé) | 4.9 % |
| Rotation annuelle (× capital) | 96 |
| Exposition brute / nette / bêta moyennes | 0.33 / 0.03 / 0.01 |
| Positions moyennes | 16.1 |
| Stops catastrophe déclenchés par an | 102 |

### Par année

| Année | Rendement | Sharpe | Drawdown max | P&L jambe acheteuse | P&L jambe vendeuse | Funding | Coûts | Rotation | Stops |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 2023 | 4.1 % | 1.74 | -3.5 % | 6.8 % | -4.6 % | 2.1 % | 0.2 % | 4 | 35 |
| 2024 | 14.1 % | 1.32 | -11.7 % | 11.2 % | 5.4 % | 2.1 % | 4.9 % | 98 | 78 |
| 2025 | 16.9 % | 1.13 | -9.1 % | -20.2 % | 37.6 % | 8.8 % | 9.8 % | 184 | 146 |
| 2026 | -2.2 % | -0.65 | -5.2 % | -2.4 % | -1.3 % | 2.1 % | 0.6 % | 11 | 56 |

## Tests statistiques

- **Probabilistic Sharpe Ratio** (vrai Sharpe > 0) : 0.968.
- **Deflated Sharpe Ratio** (34 essais effectifs comptés) : 0.312.
- **Nul par permutation** : Sharpe réel au percentile 100 %, p-valeur exacte 0.040 ; 95ᵉ percentile du nul 0.42 (24 répliques).
- **Test SPA de Hansen** (p-valeur, H0 : aucun avantage) : 0.067.
- **PBO** sur la grille : 0.452.
- **Historique minimal** pour conclure à 95 % : 891 jours.
- **Stress** : coûts ×2 → Sharpe 0.20 ; une barre de latence → Sharpe 0.56 ; stops exécutés au pire → Sharpe -0.04.
- **Concentration** : sans ses 5 meilleurs jours, Sharpe 0.40 et rendement annualisé 3.3 %.
- **Sans stops catastrophe** (diagnostic) : Sharpe 0.93, rendement annualisé 10.1 %, drawdown max -10.7 %.

### Économie du signal sans arrêt (diagnostic, hors porte)

Même stratégie sur toute la période, contrôles de drawdown et de perte journalière désactivés : ce que le signal rapporte et coûte réellement, année par année.

- Sharpe 1.03, CAGR 14.2 %, drawdown max -11.9 % ;
- P&L brut 12.4 %/an contre coûts 3.9 %/an, rotation 72×/an, exposition brute moyenne 0.43 ;
- par année : 2023 : 4.1 % ; 2024 : 15.8 % ; 2025 : 27.8 % ; 2026 : -2.1 %.

### Grille de construction (base du PBO)

| Configuration | Sharpe | CAGR | Drawdown | Rotation |
|---|---:|---:|---:|---:|
| `{'holding_horizon': 16, 'cost_aversion': 0.5}` | 1.07 | 15.8 % | -17.3 % | 264 |
| `{'holding_horizon': 16, 'cost_aversion': 1.0}` | 1.23 | 18.6 % | -12.4 % | 146 |
| `{'holding_horizon': 16, 'cost_aversion': 2.0}` | 1.09 | 13.8 % | -10.2 % | 59 |
| `{'holding_horizon': 48, 'cost_aversion': 0.5}` | 0.87 | 11.3 % | -18.1 % | 182 |
| `{'holding_horizon': 48, 'cost_aversion': 1.0}` | 0.83 | 10.6 % | -17.1 % | 151 |
| `{'holding_horizon': 48, 'cost_aversion': 2.0}` | 0.84 | 10.4 % | -12.7 % | 96 |
| `{'holding_horizon': 96, 'cost_aversion': 0.5}` | 0.49 | 4.8 % | -18.6 % | 153 |
| `{'holding_horizon': 96, 'cost_aversion': 1.0}` | 0.46 | 4.2 % | -17.8 % | 133 |
| `{'holding_horizon': 96, 'cost_aversion': 2.0}` | 0.41 | 3.3 % | -16.6 % | 106 |

## Variables les plus utilisées (gain LightGBM, moyenne des plis)

`amihud_long` 69.1 %, `cs_amihud_long` 64.4 %, `volvol_long` 63.0 %, `log_dollar_volume` 62.6 %, `funding_sum_43200m` 59.2 %, `beta` 56.5 %, `cs_vol_level` 56.4 %, `cs_beta` 55.8 %, `cs_funding_sum_43200m` 55.1 %, `skew_long` 52.2 %, `kurt_long` 50.7 %, `ret_43200m` 49.7 %, `iret_43200m` 47.2 %, `funding_sum_10080m` 43.6 %, `cs_funding_sum_10080m` 40.2 %

## Lecture honnête

Un backtest, même hors échantillon, reste une estimation : l'intervalle de confiance du Sharpe ci-dessus dit à quel point. La porte de promotion est volontairement sévère ; un résultat qui ne la franchit pas ne trade pas en réel, quel que soit l'attrait des chiffres. Un résultat qui la franchit démarre en papier, puis en réel à capital réduit, et reste surveillé par l'IC réalisé en continu.
