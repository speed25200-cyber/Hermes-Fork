# Étude des leviers 10×, 15× et 20× (24 septembre 2026)

Prototypes de recherche archivés pour la reproductibilité ; résultats et conclusions dans `docs/RESULTS.md`, § 17.
Ce code n'est pas utilisé par le système de trading et n'est pas couvert par la CI.

- `carry/`, `carry_verify/` : portage comptant/perpétuel (long au comptant, court en perpétuel), marge OKX
  multi-devises, intérêts d'emprunt USDT horaires (API OKX), liquidation intrabougie ; rejeu minute sur les
  fenêtres de squeeze (données 1 min OKX).
- `calbasis/`, `calbasis_verify/` : écart perpétuel / contrats trimestriels USDT-M Binance, marge croisée ou isolée,
  paliers de marge OKX réels.
- `xvenue/`, `xvenue_verify/` : arbitrage d'écart de funding et de prix entre Binance et OKX, marge séparée par
  plateforme, transferts de garantie retardés.
- `directional/`, `directional_verify/` : grille de 288 réglages directionnels BTC/ETH (tendance, retour à la
  moyenne, volatilité cible) avec stops dans la distance de liquidation, simulateur indépendant de contrôle.

Les données (archives data.binance.vision, API publiques OKX) ne sont pas versionnées : chaque dossier les
télécharge (`download.py`, `fetch_data.py`, `build_panel.py`) ; les chemins absolus des scripts pointent vers le
dossier de travail d'origine et sont à adapter.
