# Runbook — carnet invalide / resynchronisation

**Symptôme** : `book_invalid_total` ou `book_sequence_gaps_total` augmente ; décisions `BOOK_INVALID` ;
composant `market_data` en WARN/FAULT.

**Cause** : trou de séquence (`prevSeqId ≠ seqId`), reset non supporté, carnet croisé, quantité invalide.

**Ce que le système fait seul** : marque le book INVALID, bloque les décisions concernées, se
désabonne/réabonne au canal, attend un nouveau snapshot de la même famille de flux, revalide, puis
republie une version. Un snapshot REST n'est jamais fusionné avec des incréments WS.

**Procédure opérateur**

1. Vérifier la santé de la connexion (Santé → wsPublic) : un marché immobile n'est pas une panne.
2. Si les gaps sont répétés : consulter `GET /api/v1/data/quality` (canal, instrument, fréquence).
3. Vérifier la charge machine (`disk_free_bytes`, CPU) : une file saturée produit des drops comptés.
4. Si l'instrument reste INVALID > 5 min : le retirer temporairement via `HERMES`-like liste imposée ?
   Non — utiliser `okxq control pause --scope <inst_id>` (SOFT_HALT par instrument) et ouvrir un incident.
5. Reprise : automatique quand le book est revalidé ; sinon `okxq control resume` après vérification.
