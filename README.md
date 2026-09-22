# Hermes

Système de trading quantitatif **long/short sur contrats perpétuels crypto** (USDT-margined), reconstruit
de zéro en septembre 2026 : données sans biais de survie, apprentissage automatique validé hors
échantillon, portefeuille sensible aux coûts, gestion du risque en couches, exécution maker d'abord sur OKX.

> **Aucune rentabilité n'est promise.** Le système mesure, avec des statistiques qui savent dire « non »,
> et refuse de trader de l'argent réel avec un modèle qui n'a pas franchi sa porte de promotion. Les
> résultats mesurés sur données réelles sont dans [`docs/RESULTS.md`](docs/RESULTS.md).

## Ce qu'il fait

1. **Prédit le rendement relatif** de chaque contrat liquide sur 4 à 24 heures (net du funding, résiduel du
   marché), avec un ensemble LightGBM + Ridge (+ Transformer à attention transversale en option) entraîné
   en walk-forward purgé sur des archives Binance depuis 2020, contrats délistés compris.
2. **Construit un portefeuille bêta-neutre** : acheteur de ce qui devrait surperformer, vendeur à découvert
   de ce qui devrait sous-performer — il surfe sur les montées *et* les descentes sans parier sur la
   direction du marché. Optimiseur moyenne-variance avec coûts de transaction (zone de non-trading),
   volatilité cible, plafonds par contrat.
3. **Dimensionne selon la preuve** : la taille des positions suit l'IC réalisé en continu ; si le signal
   s'éteint, le livre s'éteint.
4. **Borne les pertes** : réduction progressive du risque en drawdown puis arrêt, disjoncteur journalier,
   plafond d'expected shortfall, interrupteur d'urgence, stops catastrophe côté OKX, dead-man switch.
5. **Exécute proprement** : ordres post-only au meilleur prix avec réalignement, bascule en IOC à glissement
   borné, réconciliation avec l'exchange, clés jamais écrites ailleurs que dans l'environnement du service.

## Démarrer

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"            # ".[dev,deep]" pour le réseau profond
python -m pytest -q                 # tests hermétiques (marché synthétique, faux serveur OKX)

hermes data download -c configs/research.yaml          # archives Binance (≈ 450 contrats, 2020 → aujourd'hui)
hermes research run  -c configs/research.yaml --out reports/essai
hermes model install reports/essai/model                # devient le champion
hermes live run -c configs/paper.yaml --mode paper      # papier sur flux live
```

Déploiement sur le VPS et exploitation : [`docs/OPERATIONS.md`](docs/OPERATIONS.md).

## Documentation

| Document | Contenu |
|---|---|
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | Chaîne complète, modules, conventions anti-biais |
| [`docs/RESEARCH.md`](docs/RESEARCH.md) | État de l'art (2013-2026) qui a guidé chaque choix, avec références |
| [`docs/RESULTS.md`](docs/RESULTS.md) | Résultats mesurés sur données réelles, hors échantillon, nets de coûts |
| [`docs/OPERATIONS.md`](docs/OPERATIONS.md) | Modes paper/démo/réel, déploiement, arrêt d'urgence, paramètres |
| `reports/` | Rapports de recherche générés et registre des essais (`trials.jsonl`) |

## Arborescence

```
src/hermes/
  data/        archives Binance, univers point-in-time, flux live, marché synthétique
  features/    ~130 variables causales
  labels/      cibles résiduelles nettes du funding, triple barrière
  models/      LightGBM, Ridge, Transformer transversal, bundle sérialisé
  validation/  splits purgés, CPCV, DSR, PSR, PBO, SPA, bootstrap
  research/    jeu de données, walk-forward, évaluation, porte de promotion, rapport
  portfolio/   coûts, covariance, alpha, optimiseur
  risk/        couche de risque
  backtest/    simulateur à frictions réalistes
  execution/   client OKX, broker OKX, broker papier
  live/        moteur, état SQLite, alertes
configs/       research, paper, demo, live
deploy/        installateur VPS, services systemd, réentraînement hebdomadaire
```
