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

Sans GitHub Actions (quota épuisé, compte bloqué…), le même déploiement depuis n'importe quel poste
disposant d'un accès SSH root au VPS :

```bash
VPS=178.104.191.79 MODE=paper TRAIN=1 bash deploy/deploy.sh
# demo / live : exporter d'abord OKX_API_KEY, OKX_API_SECRET, OKX_API_PASSPHRASE dans le shell
```

Sur le VPS :

```bash
systemctl status hermes@paper            # ou demo / live
journalctl -u hermes@paper -f
cat /opt/hermes/state/paper/status.json   # équité, positions, IC estimé, risque, exécution
systemctl start hermes-retrain            # réentraîner maintenant (sinon chaque dimanche 02:30 UTC)
```

## Unité de temps

Le moteur trade l'unité de temps du modèle installé (15 min par défaut). Pour changer : entraîner avec
`configs/research_30m.yaml` ou `configs/research_1m.yaml` (ou choisir `/etc/hermes/research_config` sur le
VPS pour le réentraînement hebdomadaire), puis installer le modèle. L'exécution s'adapte seule : la phase
passive (post-only) dure au plus 15 % de la bougie, puis bascule en IOC borné. En 1 min, un cycle de
décision prend quelques secondes (≈ 3,5 s pour les variables sur 40 contrats × 12 000 bougies, ~1 Go) ;
une durée de cycle supérieure à la bougie est journalisée.

## Tableau de bord

Lecture seule, processus séparé du moteur. Si le secret `HERMES_DASHBOARD_TOKEN` est défini, il est servi
sur `http://<vps>:8899/?token=<jeton>` (puis un cookie de session) ; sinon il n'écoute que localement
(`ssh -L 8899:127.0.0.1:8899 root@<vps>` puis `hermes live dashboard`). Il affiche équité, expositions,
IC estimé, drawdown, part maker, positions, état du risque et événements, ainsi que deux contrôles de
qualité :

- **écart d'exécution** (implementation shortfall) : prix obtenu contre le prix de décision (clôture Binance
  de la bougie), pondéré par le notionnel, en points de base, frais exclus ; il inclut la base Binance/OKX.
  Le backtest suppose environ le demi-spread plus l'impact : un écart durablement supérieur signale une
  exécution plus chère que modélisée ;
- **dérive des variables** (PSI) : distribution des variables des membres sur les dernières 24 h comparée
  à celle des 90 derniers jours d'entraînement (profil stocké dans le modèle). Au-delà de 0,25 sur plus de
  10 % des variables, une note l'indique : changement de régime ou problème de données. Simple alerte,
  jamais une entrée de trading.

## Arrêt d'urgence

```bash
sudo -u hermes /opt/hermes/.venv/bin/hermes live kill --state-root /opt/hermes/state
```

Au cycle suivant, le moteur aplatit le livre en mode urgent (IOC) et s'arrête de trader. Ce contrôle a
lieu **avant** toute donnée de marché et tout calcul du modèle : il agit même si le flux Binance ou le
modèle est en panne (il est aussi rejoué après un cycle en échec). Pour reprendre (décision humaine) :
`hermes live resume --mode <mode>`. L'arrêt automatique se déclenche aussi au drawdown dur
(`risk.drawdown_hard`).

Même si le processus meurt : les ordres en attente sont annulés par OKX en moins d'une minute
(dead-man switch `cancel-all-after`) et chaque position porte un stop catastrophe côté exchange
(`risk.stop_loss_daily_sigmas` = 8 volatilités quotidiennes depuis le prix d'entrée, entre 3 % et 50 %,
déclenché sur le prix mark) : une protection pour un moteur arrêté, rarement touchée en fonctionnement.

## Incubation en papier (avant toute promotion)

Un modèle non promu peut tourner en **papier** (jamais en réel) pour accumuler un historique réellement
hors échantillon — la seule preuve que ni le walk-forward ni la porte ne remplacent :

```bash
hermes model install reports/<meilleur-essai>/model     # champion (promu=False)
sudo systemctl enable --now hermes@paper                  # sur le VPS : moteur papier
```

Suivi (tableau de bord) : l'IC réalisé en continu doit rester proche de l'IC hors échantillon de la
recherche ; l'écart d'exécution (pb) doit rester proche du coût modélisé ; la dérive des variables (PSI) doit
rester faible. Le réentraînement hebdomadaire réévalue la même configuration sur des données plus longues ;
si elle franchit un jour la porte, le mode réel devient possible — sur décision humaine, à capital réduit
(`live.capital_fraction`).

## Recherche

```bash
pip install -e ".[dev]"
hermes data download -c configs/research_15m.yaml
hermes research run -c configs/research_15m.yaml --out reports/mon-essai
hermes model install reports/mon-essai/model        # devient le champion
```

Ou le workflow **Research** sur un runner GitHub (publie le rapport sur la branche et le modèle en
artefact). Chaque essai est inscrit dans `reports/trials/` (un fichier par exécution) : le DSR compte les
configurations distinctes. Relancer une recherche dans le même dossier réutilise le walk-forward sauvegardé
tant que seuls les réglages d'évaluation changent (coûts, portefeuille, risque, seuils de la porte).

Réentraînement hebdomadaire sur le VPS : pour la **même** configuration, la nouvelle évaluation (plus de
données) remplace toujours le champion ; un champion promu qui échoue désormais la porte est **rétrogradé**
et le moteur réel aplatit le livre. Une configuration différente ne remplace un champion promu que si elle
est promue.

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
| `live.capital_fraction` | 1 (0,25 en live) | part de l'équité du compte allouée à la stratégie ; drawdown et perte journalière sont mesurés sur la NAV de cette part (rendement du compte ÷ fraction), pas sur le compte dilué. Après un virement : `hermes live resume --mode <mode>` (repart de l'équité actuelle) |
| `live.history_days` | dérivé | historique de bougies gardé en live : par défaut le préchauffage exact des variables de recherche (≈ 37 jours en 15 min, ≈ 8 jours en 1 min) |

## Sécurité

- Les clés ne vivent que dans `/etc/hermes/hermes.env` (0640 root:hermes) et les secrets GitHub ; jamais
  dans le dépôt, les configurations, les arguments ou les journaux.
- Le service tourne sous un utilisateur dédié, système de fichiers en lecture seule hors de ses dossiers.
- Un compte, un moteur : l'installateur arrête les autres modes avant d'en démarrer un.
