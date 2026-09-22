# Exploitation

## Modes

| Mode | Données | Ordres | Clés | Garde-fou |
|---|---|---|---|---|
| `paper` | Binance live | simulés (compte papier persistant) | aucune | — |
| `demo` | Binance live | compte **démo** OKX (`x-simulated-trading`) | clés démo OKX | — |
| `live` | Binance live | OKX **réel** | clés OKX | refuse un modèle non promu ; fraction de capital `live.capital_fraction` |

Ordre recommandé : `paper` (≥ 2 semaines) → `demo` (vérifie l'exécution réelle : remplissages maker,
arrondis, stops) → `live` avec `capital_fraction` 0,25, puis augmentation si le suivi confirme.

## Déployer (GitHub Actions → VPS)

1. Secrets du dépôt (Settings → Secrets and variables → Actions) : `VPS_PASSWORD` ; pour `demo`/`live` :
   `OKX_API_KEY`, `OKX_API_SECRET`, `OKX_API_PASSPHRASE` (droits *lecture + trading*, **jamais retrait**,
   restreints à l'IP du VPS) ; optionnel `HERMES_TELEGRAM_TOKEN` + `HERMES_TELEGRAM_CHAT` pour les alertes.
   Variable optionnelle `VPS_HOST` (défaut : l'adresse historique du VPS).
2. Workflow **Deploy** : choisir le mode, cocher « Lancer l'entraînement » au premier déploiement. Le code
   est copié dans `/opt/hermes`, installé dans un venv, les services systemd sont posés, l'entraînement
   tourne en arrière-plan (~1 h) et démarre le moteur dès que le premier modèle existe.
3. Workflow **VPS status** : services, état publié, journaux, derniers rapports.

Sur le VPS :

```bash
systemctl status hermes@paper            # ou demo / live
journalctl -u hermes@paper -f
cat /opt/hermes/state/paper/status.json   # équité, positions, IC estimé, risque, exécution
systemctl start hermes-retrain            # réentraîner maintenant (sinon chaque dimanche 02:30 UTC)
```

## Arrêt d'urgence

```bash
sudo -u hermes /opt/hermes/.venv/bin/hermes live kill --state-root /opt/hermes/state
```

Au cycle suivant, le moteur aplatit le livre en mode urgent (IOC) et s'arrête de trader. Pour reprendre
(décision humaine) : `hermes live resume --mode <mode>`. L'arrêt automatique se déclenche aussi au
drawdown dur (`risk.drawdown_hard`).

Même si le processus meurt : les ordres en attente sont annulés par OKX en moins d'une minute
(dead-man switch `cancel-all-after`) et chaque position porte un stop catastrophe côté exchange
(`risk.stop_loss_daily_sigmas` volatilités quotidiennes, déclenché sur le prix mark).

## Recherche

```bash
pip install -e ".[dev]"
hermes data download -c configs/research.yaml
hermes research run -c configs/research.yaml --out reports/mon-essai
hermes model install reports/mon-essai/model        # devient le champion
```

Ou le workflow **Research** sur un runner GitHub (publie le rapport sur la branche et le modèle en
artefact). Chaque configuration testée est ajoutée à `reports/trials.jsonl` : le DSR en tient compte.

## Paramètres qui comptent

| Paramètre | Défaut | Effet |
|---|---|---|
| `portfolio.vol_target_annual` | 0,20 | volatilité visée du livre quand le modèle performe à `ic_ref` |
| `portfolio.gross_max` | 2,5 | exposition brute maximale (× équité) |
| `portfolio.weight_max` | 0,15 | poids maximal par contrat |
| `portfolio.beta_neutral` | oui | exposition bêta ≈ 0 (± 5 %) sauf si le modèle de marché est promu |
| `risk.daily_loss_limit` | 3 % | au-delà : réductions seulement jusqu'au lendemain UTC |
| `risk.drawdown_soft` / `drawdown_hard` | 10 % / 25 % | réduction linéaire du risque puis arrêt |
| `risk.es_limit_daily` | 4 % | expected shortfall 97,5 % à un jour maximal |
| `risk.exchange_leverage` | 5 | levier posé sur OKX (marge croisée) ; le levier *effectif* est `gross`, bien plus bas |
| `live.capital_fraction` | 1 (0,25 en live) | part de l'équité du compte utilisée |

## Sécurité

- Les clés ne vivent que dans `/etc/hermes/hermes.env` (0640 root:hermes) et les secrets GitHub ; jamais
  dans le dépôt, les configurations, les arguments ou les journaux.
- Le service tourne sous un utilisateur dédié, système de fichiers en lecture seule hors de ses dossiers.
- Un compte, un moteur : l'installateur arrête les autres modes avant d'en démarrer un.
