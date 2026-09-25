# Nouvelles cotations : OKX à la place de Binance, et règle d'arrêt (RESULTS § 19)

Scripts (chemins absolus du dossier de travail d'origine, à adapter ; données non versionnées) :

- `scan_bybit.py` : calendrier des perpétuels Bybit (archive publique, contrats retirés compris) → `bybit_calendar.json`.
- `okx_cal.py` : calendrier des perpétuels USDT d'OKX, contrats retirés compris (fichiers de funding de tous les
  swaps jusqu'au 2025-09-07, liste courante, sondage des archives de transactions ensuite).
- `events_a.py` : classement contre les cotations Binance (couvert par Binance / OKX d'abord / OKX seul).
- `prices.py`, `age_funding.py`, `panel.py` : première transaction, bougies 1H, âge du token, funding, panneau au
  format de `newlisting/sim.py`.
- `run.py` (événements OKX seuls, règle live gelée), `combo.py` (poche Binance + OKX).
- `kill_rule/` : calibrage de la règle d'arrêt par rééchantillonnage des opérations de la règle live.
- `verify/` : relecture adverse à quatre angles (qualité des prix et du temps, méthode et regard vers l'avenir,
  contrats de pré-marché, statistiques) et critique de complétude ; synthèse dans `verify/DIGEST.txt`.

Conclusion : extension à OKX non retenue (gain de Sharpe +0,20 ± 0,37 en 2025-2026, −0,30 ± 0,41 sur 2022-2026,
baisse maximale doublée) ; résultat hors échantillon de la poche relu à 1,57 sans les perpétuels de pré-marché ;
règle d'arrêt recalibrée (60 opérations à −3 % ou moins en moyenne, ou perte > 15 % du plafond).
