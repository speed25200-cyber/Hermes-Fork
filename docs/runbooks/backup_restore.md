# Runbook — sauvegarde et restauration (§62)

**Objectif** : produire des sauvegardes dont la restaurabilité a été **prouvée**, et savoir restaurer sans
faire plus de dégâts que l'incident. Deux scripts, deux natures : `infra/backup.sh` est sûr et répétable ;
`infra/restore.sh` **écrase des données** et exige une confirmation explicite.

**Le principe qui commande tout le reste** : « une sauvegarde non restaurée en test n'est pas une preuve de
reprise » (§62). Un fichier qui grossit n'est pas une sauvegarde. `backup.sh` va aussi loin qu'un script
peut aller sans restaurer — il relit le sommaire du vidage (`pg_restore --list`), vérifie les empreintes,
et, quand le chiffrement est actif, **déchiffre l'archive en flux pour lister son tar** afin de prouver que
la phrase de passe déposée la rendra exploitable. Mais seule une restauration réelle sur une base jetable
établit le RTO. La procédure de vérification ci-dessous n'est donc pas optionnelle.

## Préconditions

- `docker` et `docker compose` présents ; `compose.yaml` dans le répertoire de déploiement (`/opt/okxq`).
- **Le service `postgres` doit tourner** pour sauvegarder : le vidage passe par
  `docker compose exec -T postgres pg_dump`. Sans lui, le script s'arrête au lieu de produire un fichier
  vide.
- `openssl` présent et une phrase de passe disponible, sinon l'archive sera **en clair** (permissions 600,
  et le script le dit explicitement). Une valeur restée au modèle (`remplacer-par-…`) est ignorée : mieux
  vaut une archive en clair annoncée comme telle qu'un chiffrement que l'on croit avoir.
- La phrase de passe vit dans `OKXQ_BACKUP_PASSPHRASE` (environnement) ou dans
  `/opt/okxq/env/postgres.env` (ou `db.env`). Elle **ne se stocke pas à côté des sauvegardes** : sinon le
  chiffrement ne protège de rien.

## Procédure — sauvegarde

```bash
cd /opt/okxq
infra/backup.sh                       # défauts : --dir /opt/okxq --out /opt/okxq/backups --keep 14
infra/backup.sh --out /mnt/sauvegardes --keep 30
infra/backup.sh --no-volumes          # base + configurations seules (vidage rapide)
infra/backup.sh --help
```

Ce que le script fait, dans l'ordre : ① `pg_dump --format=custom` ; ② relecture du sommaire
(`pg_restore --list`) — un sommaire illisible **abandonne** la sauvegarde ; ③ `config.tar.gz` avec
`configs/`, `compose.yaml` et `infra/`, **`env/` volontairement exclu** ; volumes nommés `okxq-artifacts`
(modèles, approbations signées), `okxq-data` (manifests et archive brute), `okxq-reports` ; ④ `SHA256SUMS`
vérifié ; ⑤ empaquetage, chiffrement AES-256-CBC/PBKDF2 600 000 itérations, relecture déchiffrée,
empreinte `.sha256` et `manifeste.json` **en clair** à côté ; ⑥ rétention (`--keep`).

**Les secrets ne sont pas sauvegardés.** `env/*.env` reste dehors : §62 demande base, manifests,
configurations, modèles et approbations, pas les clés. Elles se reposent par `deploy/install.sh` ou par
`docs/runbooks/key_rotation.md`. Les dupliquer dans des archives multiplierait les copies à protéger.
Corollaire : une restauration ne rend pas un déploiement fonctionnel à elle seule, il faut aussi reposer
les fichiers d'environnement.

## Procédure — vérification de restaurabilité (obligatoire, périodique)

C'est ici que la sauvegarde devient une sauvegarde.

1. **Vérifier sans rien écraser.** Contrôle d'empreinte, déchiffrement, empreintes internes, présence et
   taille de `base.dump`, affichage du manifeste. Rien n'est écrit.
   ```bash
   infra/restore.sh --file /opt/okxq/backups/okxq-<horodatage>.tar.enc --verify-only
   ```

2. **Restaurer sur une base JETABLE** — jamais sur la base d'exploitation pour un test. Noter l'heure de
   début : c'est le début du RTO mesuré.
   ```bash
   infra/restore.sh --latest --db okxq_bac_a_sable --keep-services
   # confirmation à taper exactement : RESTAURER okxq_bac_a_sable
   ```
   `--keep-services` laisse les services applicatifs actifs et n'est acceptable **que** pour un essai sur
   base jetable : sans lui, le script arrête `collector strategy risk gateway jev-worker api` avant de
   restaurer, ce qui est le bon comportement sur une base réelle. Pour automatiser l'essai :
   `OKXQ_RESTORE_CONFIRM="RESTAURER okxq_bac_a_sable" infra/restore.sh --latest --db okxq_bac_a_sable --keep-services`.

3. **Contrôler ce qui a été restauré.** Le port de PostgreSQL est publié sur la boucle locale seulement.
   ```bash
   export OKXQ_DATABASE_URL='postgresql+psycopg://okxq:<motdepasse>@127.0.0.1:5432/okxq_bac_a_sable'
   .venv/bin/okxq db current     # révision appliquée = celle du code au moment du vidage
   .venv/bin/okxq db check       # code 0 : schéma aligné sur les modèles ; code 1 : divergence
   ```
   Vérifier aussi le contenu métier, pas seulement le schéma : `okxq reports equity --config configs/<mode>.yaml`
   et `okxq reports risk --config configs/<mode>.yaml --jours 7` sur cette base. Une base qui a les bonnes
   tables et aucune ligne n'est pas une reprise.

4. **Consigner le RTO mesuré** (début de l'étape 2 → contrôles de l'étape 3 réussis) et la date de l'essai.
   C'est la seule valeur de reprise qui compte : celle qui a été mesurée. Ne pas annoncer un RPO/RTO que
   l'infrastructure n'assure pas.

5. **Nettoyer la base jetable**, de la même façon que le script crée la sienne :
   ```bash
   docker compose exec -T postgres dropdb -U okxq okxq_bac_a_sable
   ```

## Procédure — restauration réelle (DESTRUCTIVE)

`pg_restore --clean --if-exists` **supprime puis recrée** les objets de la base cible : sur la base
d'exploitation, cela détruit le journal financier courant — ordres, fills, comptabilité, événements de
risque, high-water mark, approbations. D'où la confirmation : aucun drapeau court, aucun « o/n », il faut
taper exactement `RESTAURER <nom_de_la_base>`. Une faute de frappe doit interrompre l'opération.

1. **Relever l'état réel du compte AVANT de restaurer**, hors de la base : positions ouvertes, ordres
   ouverts, protections, marge — depuis l'application OKX ou l'API en lecture. Cet inventaire écrit
   ailleurs sera la seule référence après l'écrasement.
2. `infra/restore.sh --file <archive> --verify-only` d'abord. On vérifie, on écrase ensuite : restaurer une
   archive tronquée détruirait la base courante sans rien rétablir.
3. `infra/restore.sh --file <archive>` (base cible lue dans `env/postgres.env` si `--db` est omis). Le
   script arrête les services applicatifs — restaurer sous une application vivante mélangerait deux états,
   le gateway pouvant écrire un fill dans une base à demi restaurée. `--single-transaction --exit-on-error` :
   soit tout est restauré, soit rien.
4. **Les services restent arrêtés. Ce n'est pas un oubli.** Ne pas relancer avant l'étape suivante.

## Le cas dangereux : restaurer un état antérieur alors que des positions réelles existent

C'est le scénario qui transforme une restauration en incident. Après restauration, **la base décrit le
monde d'avant la sauvegarde ; l'échange décrit le monde d'aujourd'hui.** Concrètement, la base a
« oublié » : les ordres envoyés depuis le vidage, les fills reçus, le funding payé, les événements de
risque, les actions opérateur — et surtout **le halt persisté** éventuellement posé entre-temps. Une
restauration peut donc *ressusciter* une autorisation d'entrer que l'incident avait précisément retirée.
Le high-water mark et la perte journalière restaurés sont ceux de la sauvegarde : les seuils de protection
raisonneront sur une équité qui n'existe plus.

**La réconciliation avec l'exchange doit primer sur la base restaurée.** Une restauration du disque ne
rétablit pas la position réelle du compte.

Ordre impératif :

1. **Reposer le halt immédiatement**, avant tout redémarrage des rôles écrivains :
   ```bash
   .venv/bin/okxq control pause --config configs/<mode>.yaml \
     --reason restauration_base_etat_anterieur --actor <nom>
   ```
   Poser SOFT_HALT sur la base restaurée coûte une commande ; l'oublier autorise des entrées calculées sur
   un monde faux.
2. **Redémarrer d'abord en lecture** (`api`), vérifier `GET /api/v1/system/status`, `okxq risk status`.
3. **Laisser la réconciliation faire autorité.** Au démarrage du `gateway`, `bootstrap_reconcile` appelle
   `gateway.startup_check()` : toute tentative envoyée sans résultat persisté devient UNKNOWN, l'exposition
   pessimiste est réservée et les intentions conflictuelles sont bloquées. Suivre
   `docs/runbooks/unknown_order.md` et `docs/runbooks/reconcile_pnl.md` jusqu'à ce que
   `reconciliation_gap_usdt` soit sous le seuil.
4. **Comparer l'inventaire relevé à l'étape 1 de la restauration** avec ce que la base restaurée contient.
   Tout écart est un fait à enregistrer par une transaction tracée (`external` ou `correction`, §38), pas à
   corriger à la main.
5. **Ne lever le halt qu'ensuite**, avec `okxq control resume` et ses préconditions.

Si l'incident n'a pas corrompu la base, **il est souvent plus sûr de ne PAS restaurer** et de laisser la
réconciliation rattraper l'écart. Restaurer un état antérieur sur un compte vivant est un choix à motiver,
pas un réflexe.

## Vérifications

- Sauvegarde : `okxq-<horodatage>.tar[.enc]`, son `.sha256` et son `.manifeste.json` présents, en 600 ;
  `secrets_inclus: false` dans le manifeste ; la relecture annoncée par le script a réussi.
- Restaurabilité : `--verify-only` passe **et** un essai réel sur base jetable a eu lieu, daté, avec RTO.
- Après restauration réelle : nombre de tables non nul (le script échoue sinon), `okxq db check` aligné,
  `okxq reports equity` cohérent avec le relevé OKX, halt reposé, réconciliation terminée.

## Cas d'échec

| Message | Signification | Geste |
|---|---|---|
| `service postgres non démarré` | pas de base à vider | démarrer `postgres`, puis resauvegarder |
| `sommaire illisible : vidage corrompu` | `pg_restore --list` a échoué | sauvegarde abandonnée ; vérifier disque et version majeure |
| `phrase de passe encore au modèle : ignorée` | valeur `remplacer-par-…` | poser une vraie phrase, sinon archive en clair |
| `l'archive chiffrée ne se relit pas` | phrase de passe erronée | l'archive est supprimée ; corriger la phrase **maintenant**, pas le jour de l'incident |
| `empreinte SHA-256 incohérente` | archive altérée ou incomplète | ne pas restaurer ; prendre l'archive précédente |
| `archive chiffrée et aucune phrase de passe disponible` | phrase perdue | l'archive est irrécupérable ; c'est une perte de données |
| `confirmation incorrecte : restauration ABANDONNÉE` | texte mal tapé | rien n'a été modifié ; retaper `RESTAURER <base>` |
| `pg_restore a échoué` | transaction unique annulée | la base cible est restée dans son état antérieur |
| `base restaurée vide` | restauration considérée comme échouée | ne pas redémarrer ; reprendre une autre archive |
| `entrée non interactive et OKXQ_RESTORE_CONFIRM absent` | appel depuis un script | attendu : la restauration ne s'automatise pas par accident |

## Ce qui reste à faire par un humain

- **Décider** restaurer ou réconcilier vers l'avant. Le script ne redémarre rien à votre place, exprès.
- **Mesurer et publier RPO/RTO** à partir d'essais réels, avec leurs hypothèses. Ne pas annoncer une
  reprise sans perte que l'infrastructure n'assure pas.
- **Conserver la phrase de passe hors ligne**, ailleurs que sur le serveur sauvegardé, et garder l'ancienne
  tant que des archives chiffrées avec elle sont retenues.
- **Décider du déploiement du contenu de l'archive** : `config.tar.gz`, `artifacts.tar.gz`, `data.tar.gz`
  et `reports.tar.gz` ne sont **pas** déployés automatiquement ; les écraser pourrait remplacer un modèle
  promu par un modèle plus ancien. Les extraire explicitement si besoin (`docs/runbooks/model_rollback.md`).
- **Reposer les fichiers d'environnement** : ils ne sont pas dans l'archive (`docs/runbooks/key_rotation.md`).
- **Lacune à connaître** : la sortie finale de `infra/restore.sh` recommande
  `okxq risk reconcile --config configs/paper.yaml`. **Cette commande n'existe pas** : `okxq risk` n'offre
  que `status`. La réconciliation est déclenchée par le démarrage du `gateway` (étape `bootstrap_reconcile`)
  et s'observe via `GET /api/v1/orders`, `GET /api/v1/system/status`, `okxq risk status` et
  `okxq reports risk`. Il n'existe **aucune commande de réconciliation à la demande**.
