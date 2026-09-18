# Runbook — retour arrière d'un modèle

**Quand** : dérive de calibration, érosion, erreur de promotion, incident.

**Procédure**

1. Identifier la version cible : `okxq research evaluate --run-id <RUN_ID>` ou `GET /api/v1/models`.
   Un rollback restaure **ensemble** : modèle, transformateurs, schéma de features, politique de coûts.
   Un modèle sans artefacts complets (hash vérifié) est inutilisable.
2. Retirer la version courante : statut `RETIRED` avec motif (registre, action tracée).
3. Promouvoir la version cible seulement si son statut et ses preuves le permettent
   (`VALIDATED_OFFLINE` pour PAPER/SHADOW ; `DEMO_TECH_VALIDATED` pour DEMO ; `LIVE_APPROVED` pour LIVE).
4. Redémarrer le processus `strategy` (le modèle est chargé au démarrage, vérifié par hash).
5. Vérifier la première décision (`GET /api/v1/decisions?limit=1` : `model_id` attendu).
