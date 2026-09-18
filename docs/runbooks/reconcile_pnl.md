# Runbook — mismatch de réconciliation financière

**Symptôme** : `reconciliation_gap_usdt` non nul au-delà du seuil, événement de risque
`RECONCILIATION_MISMATCH`, prises de risque bloquées.

**Ce que le système fait seul** : journalise montant, âge, origine probable (fill manquant, funding non
enregistré, dépôt/retrait externe, correction fournisseur), retente le rapprochement avec fenêtres, bloque
les nouvelles entrées si seuil/délai dépassés. Il n'écrase JAMAIS le ledger par le dernier solde.

**Procédure**

1. Lire `GET /api/v1/risk/events?reason_code=RECONCILIATION_MISMATCH` et `okxq reports export --run-id`.
2. Comparer `account_snapshots` (source ledger vs exchange) sur la fenêtre ; vérifier `bills` OKX pour un
   flux externe ou un funding manquant.
3. Flux externe observé → enregistrer une transaction `external` (via action opérateur tracée), qui
   neutralise la performance et le HWM (T45).
4. Fill manquant → la réconciliation des fills (`fills-history` avec recouvrement) doit l'ingérer ; sinon
   ouvrir un incident et laisser le blocage actif.
5. Ne pas « corriger à la main » un montant sans transaction `correction` datée et motivée.
