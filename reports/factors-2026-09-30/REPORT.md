# Recherche des six facteurs figés

Décision historique : **cash**. Meilleur candidat de calibration : **basis**.
Aucun modèle n'est promu et aucun processus de trading n'est activé.

| Candidat | Calibration net | Confirmation net | Test net | Test coûts ×2 |
|---|---:|---:|---:|---:|
| carry | -1.772 % | +0.215 % | -0.725 % | -1.099 % |
| basis | -0.111 % | +2.055 % | -3.620 % | -3.665 % |
| reversal | +0.315 % | -1.780 % | -0.093 % | -0.212 % |
| momentum | +3.177 % | +2.648 % | +0.348 % | +0.010 % |
| flow | +3.808 % | -0.108 % | -0.208 % | -0.295 % |
| combined | +1.676 % | -0.743 % | -0.438 % | -0.530 % |

Le choix utilise uniquement la borne inférieure de moyenne en calibration, puis son rendement net. La confirmation et le test ne choisissent jamais de remplaçant. Le cash reste le choix si aucun candidat ne franchit la règle déclarée.

Verdict : `cash_no_calibration_edge`. Multiplicité déclarée : 29 essais. Données synthétiques : False.

Les comptes partent sans position au début de la calibration et restent continus entre les fenêtres. Frais, spread, impact, funding et stops suivent le moteur partagé; le stress double les coûts payés. Les données Binance ne prouvent pas les résultats de fills réels sur OKX.

Les dates et le bassin de candidats ont déjà été examinés. Ces chiffres sont rétrospectifs; ils ne démontrent pas la rentabilité future. Le pointeur shadow reste exploratoire même si la sélection économique conclut au cash.

`declaration.json` et les six entrées `trials/` précèdent toutes les simulations. `report.json` contient les empreintes de données, code, versions, scores et IC. Les CSV quotidiens sont dans `daily/`; les séries volumineuses sont ignorées par Git dans `local/`.
