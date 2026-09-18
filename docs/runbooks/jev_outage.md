# Runbook — panne JEV (TypeSafe)

**Symptôme** : `jev_error_total` augmente, `jev/status` = indisponible, circuit breaker ouvert, features
`jev_*` MISSING avec raison.

**Ce que le système fait seul** : JEV est hors chemin critique. Les positions restent protégées ; le risque
n'est pas affecté. Selon `jev.on_unavailable` : bascule vers une politique quant-only PRÉVALIDÉE si elle
existe, sinon suspension des nouvelles augmentations (`JEV_FALLBACK`). Aucune réponse tardive n'est
antidatée.

**Procédure opérateur**

1. `okxq jev status` : raison (401 clé, 429 débit, 5xx, deadline, budget journalier épuisé).
2. Budget épuisé : c'est voulu ; attendre le jour UTC suivant ou relever `max_daily_spend_usd` après revue.
3. Clé refusée : rotation via `docs/runbooks/key_rotation.md` (env du seul `jev-worker`).
4. Fournisseur en panne : rien à faire ; vérifier que les décisions portent `JEV_FALLBACK` et que la
   politique de secours est celle validée (`allow_unvalidated_fallback=false`).
5. Après retour : le circuit se referme seul après le cooldown ; vérifier le cache hit ratio.
