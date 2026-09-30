# Recherche économique — six facteurs fixes, 30 septembre 2026

**Aucun des six candidats ne démontre un avantage suffisant selon le protocole déclaré. La sélection
conclut au cash.** Aucun modèle n'est promu, aucun compte n'est démarré et le serveur reste inchangé.
Le nouveau moteur de recherche permet de tester les hypothèses sans entraîner un autre modèle opaque
ni choisir un gagnant après avoir vu les périodes de vérification.

Le compte papier existant vaut 9 369,26 USDT à 11:00 UTC, sur un capital initial déclaré de 10 000 USDT.
Son IC estimé est nul. Le [premier audit](AUDIT_TRADING_2026-09-30.md) et le rejeu de l'ancien modèle ont
montré que les correctifs d'architecture ne suffisent pas à créer un signal rémunérateur.

## Hypothèses et protocole

Les règles suivantes sont définies avant les simulations, avec coefficients fixes et IC initial nul :

| Règle | Hypothèse économique |
|---|---|
| carry | Acheter les contrats à faible funding réalisé sur sept jours et vendre ceux à funding élevé |
| basis | Acheter les perpétuels décotés et vendre ceux dont la prime est élevée |
| reversal | Retour à la moyenne des mouvements résiduels sur quatre heures et un jour |
| momentum | Persistance du mouvement résiduel sur sept jours, avec retrait du terme récent à un jour |
| flow | Flux acheteur agressif relatif qui n'est pas encore reflété dans le rendement résiduel |
| combined | Moyenne des cinq règles brutes, puis standardisation transversale |

Les formules exactes, transformations, coefficients et configuration sont dans
[`preregistration.json`](../reports/factors-2026-09-30/preregistration.json) (11:34:57 UTC), puis dans la
[`déclaration du moteur`](../reports/factors-2026-09-30/declaration.json) créée avant toute simulation.
Il n'y a eu ni ajustement des coefficients ni modification des fenêtres après lecture des résultats.

- Archives Binance, 194 contrats disponibles dans le bassin historique de 198 candidats ; univers de
  30 membres recalculé causalement, avec calendrier historique des cotations OKX. Le bassin hérité reste
  une limite : ce calcul ne réintroduit pas les contrats omis par sa constitution.
- Bougies de 30 minutes, initialisation depuis le 1er juillet 2025. Un seul livre, horizon 24 heures,
  neutralisation bêta/style, aversion aux coûts 2 et plafonds de risque inchangés. Les neuf variantes de
  livres de l'ancien modèle ne sont pas recherchées une nouvelle fois.
- Compte simulé de 10 000 USDT sans position au 1er octobre 2025, puis continu entre les trois fenêtres.
  Calibration : octobre–décembre 2025 ; confirmation : janvier–avril 2026 ; test : mai–30 août 2026.
  Le 31 août incomplet a été exclu avant simulation. Les fenêtres ne sont pas des comptes redémarrés.
- Six essais enregistrés avant le premier backtest ; 23 configurations distinctes étaient déjà présentes
  dans le registre. Bootstrap stationnaire de 10 000 réplications, blocs moyens de sept jours,
  graine 20260930 ; seuil unilatéral 0,05 / 29. Ce comptage ne prouve pas que toute recherche humaine
  antérieure a été recensée.
- Classement sur la seule calibration : borne inférieure de rendement quotidien moyen, puis rendement
  net, puis nom. Il faut un gain net positif, une borne strictement positive et un gain encore positif
  quand les frais, le spread et l'impact sont doublés. Sinon, cash. Confirmation et test ne choisissent aucun remplaçant.
- Frais, spread, impact, slippage, funding et stops suivent le moteur partagé. Les labels contiennent
  déjà le funding : il n'est pas ajouté une seconde fois à l'alpha. Douze simulations ont été exécutées,
  soit chaque candidat avec coûts normaux et avec frais/spread/impact doublés. Ce stress ne double
  ni le funding ni l'écart au VWAP d'exécution ; « coûts ×2 » désigne précisément ce scénario.

## Résultats nets

| Candidat | Calibration | Confirmation | Test | Test, coûts ×2 |
|---|---:|---:|---:|---:|
| carry | −1,772 % | +0,215 % | −0,725 % | −1,099 % |
| basis | −0,111 % | +2,055 % | −3,620 % | −3,665 % |
| reversal | +0,315 % | −1,780 % | −0,093 % | −0,212 % |
| momentum | +3,177 % | +2,648 % | +0,348 % | +0,010 % |
| flow | +3,808 % | −0,108 % | −0,208 % | −0,295 % |
| combined | +1,676 % | −0,743 % | −0,438 % | −0,530 % |

Toutes les bornes inférieures en calibration sont négatives. `basis` arrive premier au classement
parce que son compte est presque à plat : exposition brute moyenne de 0,23 % en calibration, contre
77,85 % pour `momentum`. Sa borne de −0,595 point de base par jour est moins mauvaise que celle des
autres candidats. Cela ne transforme ni sa perte ni son faible risque en avantage économique.
Le pointeur `shadow_best.json` le désigne seulement pour l'inspection du modèle ; la sélection demeure cash.

`momentum` est positif dans les trois fenêtres, avec +6,28 % sur l'ensemble octobre 2025–août 2026,
mais son drawdown maximal atteint −11,31 %. Sur le test, son gain passe de +0,348 % à +0,010 % lorsque
les frais, le spread et l'impact doublent. Sa borne de calibration est −15,42 points de base par jour : il ne satisfait
pas le protocole. Le retenir maintenant parce que ses périodes suivantes sont positives serait une
nouvelle sélection sur des résultats déjà vus. Il reste une hypothèse pour une étude future déclarée,
pas un champion validé.

Ces rendements et leurs bornes sont rétrospectifs. Les dates avaient déjà été examinées pour l'ancien
modèle. Le bootstrap suppose une stationnarité approximative ; avec le seuil retenu, seulement environ
17 réplications occupent la queue inférieure. Les coûts Binance modélisés ne prouvent pas ceux des
fills OKX. Des coefficients simples et une bonne traçabilité ne suppriment pas ces limites.

L'audit indépendant reproduit les équités et rendements et ne trouve pas de dépendance aux données
futures lors d'un recalcul sur préfixe temporel. Il relève néanmoins **3 072 cellules de prime absentes
sur 480 960 cellules membres** (0,639 %), dont les 30 contrats sur toutes les bougies du 29 juin 2026.
L'imputation neutre de Ridge transforme alors les scores `basis` en zéro ; le portefeuille continu
conserve de l'exposition et perd 0,07648 % ce jour-là. Les résultats `basis` et `combined` doivent être
lus avec cette limite de couverture. Les sorties originales sont conservées ; un prochain protocole
doit qualifier ou refuser une telle panne avant l'évaluation. Les conclusions cash restent inchangées.

Le décompte de 29 essais est partiel : le dépôt décrit d'autres recherches, dont des réglages
directionnels, hors des 23 configurations distinctes du registre. Même sans correction de multiplicité,
les six bornes individuelles à 5 % restent négatives. Le refus ne dépend donc pas du seul choix de 29.

## Architecture livrée et reproduction

`hermes research factors` refuse une sortie existante, exige la couverture des barres déclarées,
enregistre toutes les hypothèses et sauvegarde les empreintes des données, du code, des scores et de
l'IC. Les six modèles utilisent le même format de bundle que le moteur papier, avec la même précision
et le même ordre de features. Ils restent non promus même sur données synthétiques ou résultats positifs.

Les facteurs utilisent une époque de signal persistée : changer de règle ou de configuration sépare
leurs nouveaux scores/IC des anciens, sans effacer positions, risque, financement ni historique comptable.
Une transition qui écraserait les scores d'une même barre est refusée. Le retour d'un facteur vers
un modèle ML ouvre également une nouvelle époque. Les transitions ordinaires entre deux modèles ML
conservent leur fonctionnement antérieur ; leur comparaison exige toujours des états séparés.

Après la [préparation du panneau historique](../reports/audit-2026-09-30/replay/README.md#fichiers-et-reproduction),
exécuter depuis la racine du dépôt :

```bash
OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1 \
  .venv/bin/hermes research factors artifacts/diagnostic_2026/panel \
  --config configs/research_factors.yaml \
  --protocol reports/factors-2026-09-30/protocol.json \
  --out artifacts/factors-reproduced --ledger artifacts/factors-reproduced/trials
```

Le protocole versionné reproduit le comptage historique de 23 essais antérieurs ; pour une **nouvelle**
recherche, déclarer toutes les tentatives supplémentaires. Les données brutes et bundles sont conservés
hors Git ; la commande régénère les six bundles dans la nouvelle sortie. Le panneau doit correspondre
aux empreintes publiées pour reproduire les nombres. Les nouveaux horodatages de déclaration changent
normalement les empreintes de déclaration, sans changer celles des entrées numériques ni les rendements.

Le profil `configs/paper_factor_challenger.yaml` prépare un état isolé, une taille adaptative et la poche
cotations désactivée. Il ne pointe sur aucun modèle installé. En cas d'étude prospective ultérieure,
passer explicitement le bundle choisi à `--model` ; fixer avant démarrage les dates, critères et coûts
à mesurer. Aucune installation ni commande de lancement n'a été exécutée ici.

Preuves : [`report.json`](../reports/factors-2026-09-30/report.json),
[`daily/`](../reports/factors-2026-09-30/daily/),
[`inputs.json`](../reports/factors-2026-09-30/inputs.json),
[`revue indépendante`](../reports/factors-2026-09-30/independent_review.json) et les six essais du registre.
Le rapport inclut tous les perdants et chaque fenêtre, sans promotion automatique.

Validation logicielle finale : **308 tests réussis, aucun échec, aucune erreur ni test ignoré**, en
228,5 secondes ; Ruff lint/format et les contrôles du diff passent. Les deux avertissements attendus
concernent le chargement des anciens bundles sans empreinte de configuration. Le résultat et les
empreintes des sources/tests figurent dans [`validation.json`](../reports/factors-2026-09-30/validation.json).
Les transitions entre facteurs et modèles ML ont également été revérifiées indépendamment. Ces tests
sont locaux et hermétiques ; aucun test d'ordre connecté n'a été exécuté.

## Septembre non évalué

Une extension récente était déclarée uniquement pour le candidat choisi sur la calibration, sous
réserve de données complètes. Le funding réalisé OKX a été collecté pour 166 contrats avec couverture
complète ; FET n'est couvert qu'à partir du 5 septembre et 31 mappings manquent. Les inconnus restent
distincts des périodes sans règlement.

Seulement **49 des 10 788 archives de prix/premium nécessaires** ont pu être téléchargées : les deux
points d'accès officiels renvoient durablement des erreurs 503 du transport réseau. Le financement REST
Binance est également indisponible avec un code 451. L'[état de collecte](../reports/factors-2026-09-30/september_data_status.json)
est conservé. Aucun rendement septembre n'a été calculé à partir de ces fragments. L'extension aurait
en outre mélangé prix Binance et funding OKX, avec une limite de comparabilité explicite.

La rentabilité demandée reste non démontrée. Le résultat exploitable est une chaîne reproductible
qui élimine ces faux gagnants, garde le capital sans position dans sa décision de recherche, et permet
une future observation prospective sur des données complètes et des coûts de la bonne place.
