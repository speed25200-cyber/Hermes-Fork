# Exploitation

## Modes

| Mode | Données | Ordres | Clés | Garde-fou |
|---|---|---|---|---|
| `paper` | Binance live | simulés (compte papier persistant) | aucune | — |
| `demo` | Binance live | compte **démo** OKX (`x-simulated-trading`) | clés démo OKX | — |
| `live` | Binance live | OKX **réel** | clés OKX | refuse de trader un modèle non promu (il ferme alors le livre et reste à plat) ; fraction de capital `live.capital_fraction` |

Ordre recommandé : `paper` (≥ 2 semaines) → `demo` (vérifie l'exécution réelle : remplissages maker,
arrondis, stops) → `live` avec `capital_fraction` 0,25, puis augmentation si le suivi confirme.

## Déployer (GitHub Actions → VPS)

1. Secrets du dépôt (Settings → Secrets and variables → Actions) : `VPS_PASSWORD` ; pour `demo`/`live` :
   `OKX_API_KEY`, `OKX_API_SECRET`, `OKX_API_PASSPHRASE` (droits *lecture + trading*, **jamais retrait**,
   restreints à l'IP du VPS) ; optionnel `HERMES_TELEGRAM_TOKEN` + `HERMES_TELEGRAM_CHAT` pour les alertes.
   Variable optionnelle `VPS_HOST` (défaut : l'adresse historique du VPS).
2. Workflow **Deploy** (`.github/workflows/deploy-vps.yml` ; ce nom de fichier est celui déjà enregistré sur la
   branche par défaut, condition pour le lancer à la main depuis une autre branche) : choisir le mode. Le code
   est copié dans `/opt/hermes`, installé dans un venv, les services systemd sont posés. Le modèle :
   - `train_on_runner` (par défaut) : la configuration `model_config` (défaut `research_30m_xl_lb_sres`) est
     entraînée **sur le runner GitHub** (≈ 1 h 30, 16 Go de mémoire), puis le modèle est copié sur le VPS,
     installé comme champion, et le moteur (re)démarre ;
   - `model_run` : à la place, installer le modèle d'un run **Research** de ce même dépôt ;
   - ni l'un ni l'autre : redéploiement du code seul, le champion en place est gardé.

   Le VPS actuel a 7 Go de mémoire : il exécute le moteur (≈ 0,5 Go), pas l'entraînement (un walk-forward complet en
   demande bien davantage, et le manque de mémoire tuerait le moteur). Sous 12 Go, `install.sh` n'active pas
   le réentraînement hebdomadaire local et `retrain.sh` refuse de tourner : on réentraîne en relançant ce
   workflow.
3. Workflow **VPS status** (`vps-status.yml`) : services, état publié, journaux, derniers rapports.

Sans GitHub Actions (quota épuisé, compte bloqué…), le même déploiement depuis n'importe quel poste
disposant d'un accès SSH root au VPS :

```bash
VPS=178.104.191.79 MODE=paper TRAIN=0 bash deploy/deploy.sh   # puis copier un modèle : hermes model install
# demo / live : exporter d'abord OKX_API_KEY, OKX_API_SECRET, OKX_API_PASSPHRASE dans le shell
```

Sur le VPS :

```bash
systemctl status hermes@paper            # ou demo / live
journalctl -u hermes@paper -f
cat /opt/hermes/state/paper/status.json   # équité, positions, IC estimé, risque, exécution
systemctl start hermes-retrain            # VPS d'au moins 12 Go : réentraîner maintenant (sinon chaque dimanche)
```

## Réentraînement

Sur un petit VPS (moins de 12 Go), le walk-forward complet tourne sur un runner GitHub : workflow **Retrain**
(`.github/workflows/retrain.yml`), le 2 de chaque mois à 04:00 UTC (le funding du mois écoulé est alors publié)
ou à la demande. Il entraîne **avec le code installé sur le VPS** (`/opt/hermes/REVISION`, écrit par le
déploiement) et **la configuration du champion** (`/etc/hermes/research_config`, écrite par le déploiement),
publie le rapport en artefact, puis `deploy/challenger.sh` applique la règle champion / challenger : même
stratégie (identité recalculée par le code installé), le nouveau modèle remplace toujours l'ancien (une
rétrogradation aplatit le livre réel) ; autre stratégie, seulement s'il est promu ou si le champion ne l'est pas.
Le moteur recharge le modèle à chaud ; l'installation copie à côté puis échange par renommage. Sécurité :
l'entraînement, qui exécute des dépendances téléchargées (versions et empreintes de `requirements.lock`), tourne
dans un job sans aucun secret ; seul le job d'installation reçoit le mot de passe du VPS et il n'exécute que les
scripts du dépôt, envoyés par l'entrée standard de ssh.
Comme en recherche, où chaque pli de 60 jours est ré-entraîné, le modèle en service ne vieillit pas au-delà d'un
mois. Sur un VPS d'au moins 12 Go, `hermes-retrain.timer` fait la même chose chaque semaine, sur place.

## Unité de temps

Le moteur trade l'unité de temps du modèle installé. Le réentraînement utilise par défaut le
meilleur candidat exécutable, `configs/research_30m_xl_lb_sres.yaml` (bougies de 30 min, détention 24 h,
voir `RESULTS.md`) ; pour changer, écrire un autre chemin dans `/etc/hermes/research_config` sur le VPS (par
ex. `configs/research_15m_long.yaml` ou `configs/research_1m_long.yaml`), puis installer le modèle. L'exécution s'adapte seule : la phase
passive (post-only) dure au plus 15 % de la bougie, puis bascule en IOC borné. En 1 min, un cycle de
décision prend quelques secondes (≈ 3,5 s pour les variables sur 40 contrats × 12 000 bougies, ~1 Go) ;
une durée de cycle supérieure à la bougie est journalisée.

## Tableau de bord

Lecture seule, processus séparé du moteur : il ne peut passer, modifier ni annuler aucun ordre. Si le secret
`HERMES_DASHBOARD_TOKEN` est défini, il est servi sur `http://<vps>:8899/?token=<jeton>` : le jeton devient un
cookie HttpOnly et disparaît aussitôt de la barre d'adresse (port ouvert dans ufw s'il est actif) ; sinon il
n'écoute que localement (`ssh -L 8899:127.0.0.1:8899 root@<vps>` puis `hermes live dashboard`). Un seul service
sert tous les modes (onglets **Papier** et **Réel**, **Démo OKX** s'il a tourné) en lisant la racine des états.
Les anciens systèmes du VPS (okxq, ancien moteur Node) s'effacent avec l'option `retire_old` du déploiement.

Sections : **Terminal** (indicateurs clés, graphique des prix TradingView Lightweight Charts avec ouvertures,
sorties et stops de chaque position, lignes d'entrée et de stop, positions longues et courtes, équité face au
cône de la recherche, composition du livre, exécutions, événements), **Positions** (entrée, prix, P&L latent,
stop et distance, score, cible, ancienneté), **Historique** (positions fermées reconstruites des exécutions :
P&L net, rendement, durée, sortie par rééquilibrage ou stop ; statistiques ; exécutions), **Signaux**
(classement du modèle, poids visés, IC réalisé), **Risque** (limites face aux seuils, garde-fous, drawdown,
exposition, stops), **Modèle** (modèle en service, porte de promotion critère par critère, contrôles
pré-enregistrés), **Système** (chaîne de décision, compte, notes), **Journal**. La stratégie ne pose pas de
take-profit : les sorties se font par rééquilibrage ; le stop affiché est le stop catastrophe réellement posé.
Les bougies viennent de l'API publique Binance (mises en cache 15 s). Démonstration sur un marché
synthétique, sans réseau : `python scripts/dashboard_demo.py build /tmp/demo` puis
`python scripts/dashboard_demo.py serve /tmp/demo`.

Il affiche aussi deux contrôles de qualité :

- **écart d'exécution** (implementation shortfall) : prix obtenu contre le prix de décision (clôture Binance
  de la bougie), pondéré par le notionnel, en points de base, frais exclus ; il inclut la base Binance/OKX.
  Le backtest suppose environ le demi-spread plus l'impact : un écart durablement supérieur signale une
  exécution plus chère que modélisée ;
- **dérive des variables** (PSI) : distribution des variables en direct comparée à celle des 90 derniers
  jours d'entraînement (profil stocké dans le modèle) : les lignes des membres sur les dernières 24 h pour les
  variables propres à chaque contrat, une ligne par bougie sur les 7 derniers jours pour les variables de
  marché (calendrier, breadth, funding moyen : une seule valeur par bougie pour tous les contrats ; non lues
  en bougies d'une minute). Le seuil de chaque variable est calibré à l'entraînement : le quantile 99 % du PSI
  que ces mêmes fenêtres atteignent sur les 90 jours précédant le profil (au moins 0,25) — une référence
  mesurée sur un trimestre passé, pas un taux de fausses alertes. Un seuil fixe prenait une variable lente ou
  cyclique pour une dérive : 75 fausses alertes en papier, dont un PSI de 17 sur le jour de la semaine, arrondi
  en float16 à l'entraînement et pas en direct (les lignes en direct sont désormais arrondies comme à
  l'entraînement, et le modèle note les mêmes valeurs qu'en recherche). Au-delà du seuil sur plus de 10 % des
  variables, une note l'indique : changement de régime ou problème de données. Une variable dont plus de la
  moitié des valeurs est manquante là où l'entraînement n'en avait pas, ou sort de la plage d'entraînement de
  plus que sa largeur, est signalée à part (défaut de données probable), même pour un modèle sans seuils
  calibrés (antérieur à la calibration, ou entraîné sur trop peu d'historique : moins de 30 jours
  disponibles avant le profil). Recalculé à chaque bougie depuis l'historique chargé (pas de mémoire à
  reconstruire après un redémarrage). Simple alerte, jamais une entrée de trading : une erreur du contrôle
  est journalisée et le trading continue.

## Poche « nouvelles cotations » (papier)

`live.listing_sleeve` (activée dans `configs/paper.yaml`, jamais en mode réel) : court sur chaque nouveau
perpétuel USDT-M de Binance dont le token est neuf (aucun marché au comptant Binance, ou ouvert depuis moins de
30 jours) et qu'OKX cote, en deux tranches (24 h et 72 h après la cotation), fermées 7 jours après, couvertes par un
long BTC de même montant, avec un stop à +50 % de la première entrée ; une tranche n'entre que dans les 3 heures qui
suivent son heure (jamais rattrapée après un redémarrage) ; cinq nouveaux tokens au plus, plafond 0,8× de la NAV
(docs/RESULTS.md § 18). Aucune entrée tant que le livre est restreint (données périmées, arrêt, disjoncteur de perte
journalière, budget de drawdown entamé) ; un arrêt ferme aussi la poche. Le calendrier vient de `exchangeInfo` de Binance (relu toutes les six heures,
mis en cache avec les opérations dans la base d'état) ; le livre est décidé sur le compte net des jambes de la
poche, les deux listes de cibles sont additionnées avant exécution ; une panne de la poche ne bloque jamais le
livre. Le tableau de bord (onglet Positions) montre les shorts ouverts, le P&L, la couverture OKX (combien de
nouveaux tokens sont cotés sur OKX à l'échéance) et l'état. **Règle d'arrêt** : plus aucune entrée si les 25
dernières opérations perdent en moyenne ou plus de 10 % du plafond (P&L net du funding et de coûts de recherche,
0,15 % par côté sur le token, 0,06 % sur BTC) ; revue après 12 mois et 30 opérations.

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
| `live.history_days` | dérivé | historique de bougies gardé en live : par défaut le préchauffage exact des variables de recherche plus une semaine pour le contrôle de dérive des variables de marché (un jour en 1 min) |

## Sécurité

- Les clés ne vivent que dans `/etc/hermes/hermes.env` (0640 root:hermes) et les secrets GitHub ; jamais
  dans le dépôt, les configurations, les arguments ou les journaux.
- Le service tourne sous un utilisateur dédié, système de fichiers en lecture seule hors de ses dossiers.
- Un compte, un moteur : l'installateur arrête les autres modes avant d'en démarrer un.
