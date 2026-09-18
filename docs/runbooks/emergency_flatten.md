# Runbook — sortie d'urgence (EMERGENCY_FLATTEN)

**Quand** : perte de contrôle (état exchange inconnu prolongé, positions divergentes, incident de sécurité),
décision opérateur.

**Procédure**

1. Interface → Risque/exploitation → « Demander flatten » (confirmation contextuelle : compte + mode), ou
   `okxq control request-flatten --config configs/<mode>.yaml --reason <motif> --actor <nom>`.
2. Le système : fige les entrées, relève l'exposition la plus récente, annule les ordres d'entrée,
   soumet des réductions bornées reduce-only (limite agressive avec corridor ; market seulement si
   `emergency_market_exit_enabled`), suit les fills, réconcilie.
3. Suivre `GET /api/v1/system/status` : `FLATTEN_PENDING` / `RESIDUAL_EXPOSURE` avec résidus par instrument.
   L'écran ne montre jamais « FLAT » sans preuve de réconciliation.
4. Exchange inaccessible : les retries suivent le budget ; alertes maintenues ; intervenir sur OKX directement
   si nécessaire (l'application OKX reste l'autorité) puis relancer la réconciliation.
5. Reprise : uniquement par `request-resume` après revue d'incident ; `auto_resume_after_critical_halt=false`.
