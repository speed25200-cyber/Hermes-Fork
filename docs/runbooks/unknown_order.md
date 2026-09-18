# Runbook — ordre en état UNKNOWN

**Symptôme** : `unknown_orders_total > 0`, alerte « état de compte inconnu », `okxq risk status` montre des
réservations pessimistes actives, l'interface affiche un ordre `UNKNOWN`.

**Cause** : coupure/timeout entre l'envoi et la journalisation de l'ACK, ou réponse ambiguë d'OKX (§48, §52).

**Ce que le système fait seul** : réserve une exposition pessimiste, bloque les intentions conflictuelles,
n'envoie jamais un doublon, lance la réconciliation (ordres ouverts, `GET /trade/order` par clOrdId,
fills avec recouvrement temporel).

**Procédure opérateur**

1. `okxq risk status --config configs/<mode>.yaml` : lister les UNKNOWN et leur âge.
2. Vérifier la réconciliation dans l'interface (Risque/exploitation → réconciliation) ou `GET /api/v1/orders?state=UNKNOWN`.
3. Si l'UNKNOWN persiste au-delà du délai configuré : `okxq control pause --reason unknown_order_unresolved`
   (SOFT_HALT : aucune nouvelle entrée).
4. Vérifier manuellement sur OKX (application ou API en lecture) l'existence de l'ordre par `clOrdId`.
5. Si l'ordre n'existe pas côté exchange ET que la fenêtre de recouvrement est passée : marquer via
   `okxq control resolve-unknown` (NOT_IMPLEMENTED : à réaliser par action opérateur en base, tracée) —
   sinon attendre la réconciliation automatique suivante.
6. Ne jamais renvoyer l'ordre à la main. Ne jamais libérer la réservation sans état final observé.
