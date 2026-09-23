# Architecture d'Hermes

Hermes est un système de trading **long/short sur contrats perpétuels crypto** (USDT-margined). Il décide
sur des bougies de **1 minute, 15 minutes ou 30 minutes** (15 min par défaut), prédit le rendement *relatif*
de chaque contrat sur les prochaines minutes/heures, construit un portefeuille bêta-neutre qui achète ce
qui devrait monter et vend à découvert ce qui devrait baisser, et l'exécute sur OKX.

## Unités de temps

Toutes les fenêtres sont définies en **minutes** (variables) ou en **jours** (validation, covariance) et
converties en barres de l'unité choisie : la même bibliothèque sert le 1 min, le 15 min et le 30 min, et un
nom de variable (`ret_60m`) désigne la même chose quelle que soit la bougie.

| Unité | Horizons de prédiction | Particularités |
|---|---|---|
| 15 min (défaut) | 30 min, 1 h, 2 h — ou 4 h, 8 h, 24 h (`research_15m_long`) | agrégats 1 min optionnels dans chaque bougie (variance réalisée, sauts, asymétrie, flux de fin de barre, VWAP) |
| 30 min | 1 h, 2 h, 4 h — ou 4 h, 8 h, 24 h (`research_30m_long`) | agrégé exactement depuis le 15 min |
| 1 min | 5, 15, 30 min — ou 1 h, 4 h, 8 h avec décision toutes les 5 min (`research_1m_long`) | top 15 contrats seulement, fenêtres ≤ 3 jours, exécution en quelques secondes |

La **bougie** fixe la résolution des données et la cadence de décision (éventuellement une bougie sur k,
`portfolio.rebalance_every`, sur une grille alignée sur l'horloge identique en recherche et en live) ;
l'**horizon** fixe ce que le modèle prédit et combien de temps une position est censée être tenue. Les
deux sont indépendants : les mesures (`docs/RESULTS.md`) montrent qu'à coûts OKX un horizon court ne paie
pas ses transactions même avec un IC élevé.

L'univers point-in-time est calculé à résolution **journalière** avec des dates de resélection
calendaires : la recherche et le moteur live sélectionnent les mêmes contrats aux mêmes dates, même si le
moteur ne garde que quelques semaines de bougies de base.

Le même code sert à la recherche, au backtest, au papier et au réel : ce qui a été validé est ce qui trade.

```
  archives Binance ──► panel point-in-time ──► features causales ──► cibles résiduelles nettes du funding
  (délistés inclus)     (univers top-N)          (~130 variables)       (vol-normalisées, 2-8 barres)
                                                        │
                                                        ▼
                        walk-forward purgé : LightGBM + Ridge (+ Transformer transversal)
                                                        │  scores hors échantillon uniquement
                                                        ▼
        score lissé (EWMA) ──► IC réalisé causal ──► alpha = IC × σ_résiduelle × score  (Grinold)
                                                        │
                                                        ▼
        optimiseur moyenne-variance à coûts L1 (zone de non-trading), bêta-neutre, cible de volatilité
                                                        │
                                                        ▼
        couche de risque : drawdown progressif → arrêt, disjoncteur journalier, plafond d'ES, interrupteur
                                                        │
                                                        ▼
        exécution OKX : maker d'abord (poursuite), IOC borné, stops catastrophe serveur, dead-man switch
```

## Modules

| Module | Rôle |
|---|---|
| `hermes.data.binance_archive` | Téléchargement des archives Binance USDT-M (klines avec volume agresseur, funding, prime, métriques), cache disque, liste des contrats **y compris délistés**. |
| `hermes.data.universe` | Univers point-in-time : top-N par volume moyen des 30 jours *précédents*, ancienneté minimale, exclusion des stablecoins et des perpétuels sur actions/matières premières. |
| `hermes.data.live_feed` | Flux live Binance aux mêmes conventions que la recherche (barres clôturées uniquement), bougies journalières pour l'univers, bougies 1 min pour les agrégats intra-barre. |
| `hermes.data.intrabar` | Agrégats 1 min par bougie de base, strictement causaux. |
| `hermes.data.synthetic` | Marché synthétique à signal planté connu (tests : le pipeline doit le retrouver, et ne rien trouver dans le bruit). |
| `hermes.features.library` | ~130 variables causales, sans échelle (retours / volatilité ex ante, z-scores, rangs). |
| `hermes.labels` | Cibles résiduelles (bêta, ou bêta + styles) nettes du funding, normalisées ; triple barrière et poids d'unicité. |
| `hermes.models` | LightGBM (arrêt précoce sur l'IC transversal), Ridge, réseau à attention transversale (optionnel), bundle sérialisé sans pickle avec empreintes SHA-256. |
| `hermes.validation` | Splits purgés (walk-forward, CPCV), IC Newey-West, Sharpe de Lo, PSR, DSR, MinTRL, bootstrap stationnaire, SPA de Hansen, PBO. |
| `hermes.research` | Jeu de données (construction par tranches bornée en mémoire), walk-forward, évaluation, porte de promotion, rapport, modèle final. |
| `hermes.portfolio` | Modèle de coûts, covariance, alpha, optimiseur, construction d'un rebalancement. |
| `hermes.risk.overlay` | Couche de risque commune au backtest et au réel. |
| `hermes.backtest.engine` | Simulateur barre par barre : frais, spread, impact, funding, sorties d'univers. |
| `hermes.execution` | Client REST OKX signé, instruments et arrondis, broker OKX, broker papier. |
| `hermes.live` | Boucle de décision à chaque clôture de bougie, état SQLite, alertes, tableau de bord, rechargement à chaud du modèle. |

## Conventions qui empêchent de se mentir

1. **Temps.** Une barre est indexée par son ouverture et connue à sa clôture. Une décision prise à la
   clôture de `t` est exécutée pendant `t+1`, au **VWAP du premier quart d'heure de `t+1`** (champ
   `vwap_first`) et non au dernier prix qui a servi à la calculer ; les contrats sont dimensionnés au prix de
   décision, comme le fait le broker. Le funding réglé dans la barre `t` est payé par la position tenue
   pendant `t`.
2. **Causalité testée.** `tests/test_features.py` tronque le futur et vérifie qu'aucune valeur passée ne
   change ; un second test vérifie que les variables calculées sur la fenêtre live égalent celles de la
   recherche.
3. **Univers sans regard vers l'avant, sur la plateforme d'exécution.** Pas de « coins qui ont monté »
   choisis après coup ; et seuls les contrats qu'**OKX listait la veille** peuvent entrer (calendrier
   reconstruit jour par jour depuis les archives d'OKX, `hermes.data.venue`) : le moteur réel ne peut
   trader rien d'autre. Une revue a montré qu'un univers pris sur tout Binance attribuait un tiers du P&L
   à des contrats inexistants sur OKX.
4. **Coûts partout.** Le backtest paie frais maker/taker, demi-spread (estimateur d'Abdi-Ranaldo), impact
   en racine carrée et funding ; l'optimiseur les anticipe.
5. **Hors échantillon uniquement.** Toute mesure de performance utilise les prédictions walk-forward.
6. **Nombre d'essais compté.** Chaque configuration testée est inscrite dans `reports/trials/` et
   dégonfle le Sharpe (DSR).
7. **Trous de données comblés causalement.** Une bougie manquante est remplacée par une bougie plate (au
   plus 45 min) selon une règle qui ignore si la série reprend ensuite : identique en recherche et en live.
8. **Pas de mois sans funding.** Les archives de funding sont mensuelles : les semaines du mois en cours
   (bougies sans funding connu) sont retirées de la recherche plutôt que traitées comme un funding nul.
9. **Découpage mémoire sans fuite.** Le jeu de données par tranches calcule chaque tranche sur tous les
   contrats membres pendant la tranche *et* son préchauffage : le marché et les bêtas ne dépendent jamais
   de l'appartenance future à l'univers.

## Pourquoi ces choix (résumé ; détails et références dans `RESEARCH.md`)

- **Prédire le relatif plutôt que la direction.** La direction du marché crypto est très difficile à
  prévoir ; le rendement relatif entre contrats l'est un peu moins, se diversifie sur des dizaines de noms,
  et un livre bêta-neutre n'a pas besoin que le marché monte.
- **LightGBM comme cheval de trait.** Les comparaisons récentes rigoureuses donnent l'avantage au boosting
  sur les réseaux profonds et les modèles de fondation (Chronos, TimesFM) pour prédire des *rendements* ;
  le réseau à attention transversale est un diversifieur optionnel.
- **L'IC réalisé pilote la taille.** Le modèle ne décide pas seul de sa confiance : l'IC mesuré en continu
  (retardé de l'horizon, donc causal) dimensionne les positions ; si le signal meurt, le livre s'éteint.
- **La zone de non-trading.** Avec un IC de quelques pour cent, un signal statistiquement réel perd de
  l'argent si on le suit à chaque heure ; l'optimiseur à coûts L1 ne trade que quand l'alpha marginal
  dépasse le coût, amorti sur la durée de vie du signal (Gârleanu-Pedersen).
- **Le risque borne la perte quelle que soit la qualité du modèle.** Drawdown progressif puis arrêt,
  disjoncteur journalier, plafond d'expected shortfall, stops catastrophe côté exchange — placés loin (8
  volatilités quotidiennes depuis l'entrée, 3 % à 50 %) : ils protègent d'un moteur arrêté, ils ne sont pas
  une règle de trading. À 4 volatilités ils se déclenchaient un jour sur trois et le P&L dépendait du prix
  d'exécution supposé des stops ; sans stops, le livre fait aussi bien.
