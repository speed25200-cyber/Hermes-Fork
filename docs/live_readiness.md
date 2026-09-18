# Dossier LIVE — gates et preuves (§71)

**Statut global : LIVE NON AUTORISÉ.** `configs/live.disabled.yaml` a `live_enabled: false` ; aucun
manifeste d'approbation n'existe ; aucun gate n'est franchi. Ce document liste ce qui est requis, ce
qui est en place, et ce qui manque. Rien ici ne constitue une autorisation.

## Mécanique d'activation (implémentée, non exercée en réel)

1. L'opérateur remplit un corps de manifeste (compte, environnement LIVE, commit, hash de configuration,
   hashes d'artefacts, limites propres au capital, expiration, acteur, gates avec preuves).
2. Il le signe avec `scripts/sign_approval_manifest.py` (HMAC-SHA256, `OPERATOR_AUTH_SECRET` côté serveur).
3. `okxq doctor --config configs/live.yaml` puis le démarrage vérifient signature, expiration, commit,
   hash de configuration, gates et limites (`okxq.config.live_guard`). Un échec bloque AVANT toute
   connexion privée. Aucune route de l'API et aucune option `--force` ne contournent cette garde.
4. Même avec un manifeste valide : preflight connecté (positions initiales, ordres, marge, état privé,
   protections), démarrage à exposition bornée, pas d'augmentation automatique du capital.

## Gate technique (§71.1)

| Preuve requise | État |
|---|---|
| Tests critiques verts (`make test`, intégration PostgreSQL) | voir `DELIVERY_REPORT.md` |
| Connecteur DEMO validé (préflight connecté, ordres démo, réconciliation) | **NON EXÉCUTÉ** (aucune clé DEMO dans cette session) |
| Clés et réseau séparés par service | en place (`compose.yaml`, `infra/env/*.example`) |
| Unités/frais exacts | fixtures §68.1 ; frais réels du compte non observés |
| Réconciliation fiable | testée hors ligne ; non observée sur compte réel |
| Protections confirmées côté exchange | testées contre le simulateur et un faux adaptateur ; non observées sur OKX |
| Reprise après panne testée | scénarios chaos §68.3 hors ligne |
| Sauvegarde restaurée | procédure `infra/backup.sh`/`restore.sh` ; restauration à exercer sur la machine cible |
| Supervision active | métriques/alertes en place ; destinataire d'alerte à configurer |

## Gate scientifique (§71.2)

| Preuve requise | État |
|---|---|
| Protocole préenregistré | `configs/experiment.example.yaml` (exemple) |
| Historique réellement disponible | **AUCUN historique réel collecté** ; jeux synthétiques seulement |
| Périodes indépendantes, PnL net, coûts stressés | mécanique implémentée ; aucune mesure réelle |
| Collecte prospective (SHADOW) | à démarrer sur le VPS |
| Gain JEV distinct des métadonnées, validé prospectivement | mécanique A/B/C/D implémentée ; **aucun appel JEV réel** |

**Conclusion** : aucune preuve d'alpha. Le système reste en PAPER/SHADOW.

## Gate opérateur et compte (§71.3)

| Élément | État |
|---|---|
| Compte autorisé, profil régional, capital alloué, éligibilité produits | à renseigner par l'opérateur |
| Approbation explicite des versions (code, modèle, données, coûts, risque) | manifeste à signer |
| Procédure de perte maximale tolérée | à rédiger avec l'opérateur |
| Vérification des conditions de service et de la juridiction | à faire par l'opérateur |
