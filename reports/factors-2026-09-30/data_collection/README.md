# Collecte publique de septembre 2026

Copies exactes des scripts employés pour la collecte, sans reformatage. Ils ne font
ni évaluation de stratégie, ni accès à un compte, ni envoi d'ordre.

Prérequis : Python 3.11+ (3.12 utilisé), environnement du dépôt installé (`pip install -e .`),
notamment `httpx`, `numpy`, `pandas` et `pyarrow`, et accès HTTPS aux sources publiques.
Exécuter les commandes ci-dessous depuis la racine du dépôt.

L'entrée `artifacts/diagnostic_2026/declaration.json` fige les 198 candidats. La
[reproduction du panneau historique](../../audit-2026-09-30/replay/README.md#fichiers-et-reproduction)
documente cette déclaration, l'artefact GitHub requis et la commande
`reports/audit-2026-09-30/replay/fetch_panel.py`. Le raccord exige également les fichiers
`artifacts/diagnostic_2026/data/parsed/15m/*.parquet` et
`artifacts/diagnostic_2026/panel/venue_listed.parquet` produits par ce script.

```bash
.venv/bin/python reports/factors-2026-09-30/data_collection/fetch_september.py
```

Une fois la collecte du financement achevée (`funding_coverage.json` présent), le
raccord historique peut être préparé séparément :

```bash
.venv/bin/python reports/factors-2026-09-30/data_collection/prepare_bridge.py
```

Les sorties et caches restent dans le répertoire ignoré
`artifacts/profitability_2026/september/`. Une reprise réutilise les réponses déjà
présentes ; les réponses HTTP, dates et empreintes sont enregistrées dans `requests.jsonl`.
Ces scripts conservent leurs dates fixes de septembre 2026.

État de cette livraison : **49 archives de prix/premiums sur 10 788 téléchargées**,
collecte interrompue par des erreurs 503 de transport sur les deux miroirs officiels.
Le financement OKX couvre tout le mois pour 166 candidats ; FET est partiel et les
31 autres mappings restent inconnus. Voir [l'état enregistré](../september_data_status.json).
Il n'existe donc aucun panneau complet de prix septembre ni résultat économique septembre.

Les prix, volumes et premiums proviennent de Binance ; le financement réalisé provient
d'OKX. Ce changement de source reste explicite et séparé de l'historique financé sur
Binance. Les règlements inconnus restent `NaN`, avec `funding_known=0` ; une barre sans
règlement ne peut être valorisée à zéro que si sa couverture est connue. Le raccord
`bridge30m/` ajoute les huit barres du 31 août 20:00–23:30 UTC sans modifier le panneau
historique original. Les sources et gros fichiers de données ne sont pas inclus dans Git.

Empreintes SHA-256 des copies, identiques aux originaux :

- `fetch_september.py` : `fb5e0d084cef625fe716177b54f96bf827d093c4aad9e8fe17282231caef54d2`
- `prepare_bridge.py` : `a9f62b401e6b045fb35d490f1465935d04e44bc7cdb14227e485844f78f3dfe9`
