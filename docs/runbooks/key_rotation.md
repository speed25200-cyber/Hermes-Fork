# Runbook — rotation des secrets (§60)

**Objectif** : remplacer un secret sans interrompre la séparation qui le protège, et sans jamais le faire
transiter par un canal qui le conserve.

**Deux règles qui ne se négocient pas**

- **On ne colle JAMAIS une clé dans une conversation, une issue, un commit, un ticket ou un dépôt.** Un
  secret lu est un secret compromis : il reste dans l'historique du shell, dans les journaux du terminal,
  dans un presse-papiers, dans l'index d'un outil. C'est pourquoi `okxq api keys` n'affiche **pas** les
  clés par défaut, écrit un fichier en 0600 et ne rend qu'un chemin et une empreinte tronquée.
- **Chaque secret ne vit que dans le fichier d'environnement de SON service.** Un secret injecté
  globalement dans tous les conteneurs viole cette séparation : le rayon d'un vol de clé devient la surface
  complète de la plateforme.

Les fichiers réels vivent **sur le serveur**, dans `/opt/okxq/env/`, en 600 et hors du dépôt. Le dépôt ne
contient que des modèles `infra/env/*.env.example`, qui ne portent que des valeurs manifestement fausses.

| Secret | Seul fichier autorisé (serveur) | Modèle versionné | Seul service |
|---|---|---|---|
| `OKX_API_KEY`, `OKX_API_SECRET`, `OKX_API_PASSPHRASE`, `OKX_ACCOUNT_REGION_PROFILE` | `/opt/okxq/env/gateway.env` | `infra/env/gateway.env.example` | `gateway` |
| `TYPESAFE_API_KEY` | `/opt/okxq/env/jev-worker.env` | `infra/env/jev-worker.env.example` | `jev-worker` |
| `OPERATOR_AUTH_SECRET` | `/opt/okxq/env/api.env` | `infra/env/api.env.example` | `api` |
| `POSTGRES_PASSWORD`, `OKXQ_BACKUP_PASSPHRASE` | `/opt/okxq/env/postgres.env` (ou `db.env`) | `infra/env/db.env.example` | `postgres` |

Cette table est aussi du code : `SERVICE_OWNER` dans `scripts/security_check.py`. **L'apparition d'une clé
OKX ailleurs que dans le `gateway` est une régression de sécurité, pas une configuration** — et elle est
détectée (famille `separation_secrets`, sur `compose.yaml` analysé *après* interprétation YAML, donc un
ancrage partagé porteur de secret est développé dans chaque service et repéré). Le code refuse aussi de
démarrer : `assert_credentials_separation()` lève `ConfigError` pour tout rôle autre que `gateway`
auquel on a injecté une variable `OKX_API_*`.

## Préconditions (toutes les rotations)

- Accès `root` au serveur, `/opt/okxq/env` en 700, chaque fichier en 600.
- Ne jamais poser une valeur avec `echo SECRET=...` sur la ligne de commande : elle apparaît dans la liste
  des processus et dans l'historique. Utiliser un éditeur sur le serveur, ou `deploy/install.sh` qui lit
  les valeurs **dans l'environnement** (`OKX_API_KEY_POSE`, `TYPESAFE_API_KEY_POSE`,
  `OKXQ_OPERATOR_KEY_POSE`, …) et ne les imprime jamais.
- Ne jamais renommer un `*.env.example` en `*.env` dans le dépôt : `scripts/security_check.py` le refuse
  (famille `env_suivi_par_git`).
- Après toute rotation : `python scripts/security_check.py --json` (ou `make security-check`) doit rendre
  `"ok": true`.

---

## A. Clés OKX (`gateway.env` uniquement)

**Pourquoi un halt d'abord** : le `gateway` est le seul point de signature et d'envoi. Changer sa clé
pendant qu'une intention est en vol laisserait un ordre dont l'ACK arrive sur une connexion authentifiée
par une clé révoquée — un UNKNOWN fabriqué par nous (§52.1).

1. Créer la nouvelle clé **chez OKX**, restreinte : aucun droit de retrait, liste d'IP autorisées.
2. Suspendre les entrées et vider ce qui est en vol :
   ```bash
   .venv/bin/okxq control pause --config configs/<mode>.yaml \
     --reason rotation_cle_okx --actor <nom>
   ```
   Vérifier l'absence d'UNKNOWN (`GET /api/v1/orders?state=UNKNOWN`) avant de continuer. Si des ordres
   d'entrée doivent disparaître : `okxq control cancel-entry-orders --config ... --reason rotation_cle_okx --actor <nom>`.
3. Poser les valeurs dans `/opt/okxq/env/gateway.env` **et nulle part ailleurs** (600, propriétaire root) :
   `OKX_API_KEY`, `OKX_API_SECRET`, `OKX_API_PASSPHRASE`, et `OKX_ACCOUNT_REGION_PROFILE` si le profil
   change. Aucun autre fichier de `env/` ne doit contenir ces noms.
4. Redémarrer **seulement** le gateway :
   ```bash
   cd /opt/okxq && docker compose up -d gateway     # ou : docker compose restart gateway
   ```
   Aucun autre service ne détient ces variables ; les redémarrer serait inutile et brouillerait le
   diagnostic. Le gateway a 120 s de grâce à l'arrêt : il traite ce qui est en vol avant de repartir.
5. Vérifier **sans envoyer d'ordre** :
   ```bash
   .venv/bin/okxq demo preflight --config configs/demo.yaml
   ```
   Il contrôle clés DEMO, profil, en-tête `x-simulated-trading` et mode de compte (net/isolated). En PAPER,
   il n'y a rien à vérifier : aucune clé n'est utilisée.
6. **Révoquer l'ancienne clé chez OKX seulement après** que la nouvelle a été prouvée. Révoquer d'abord
   crée une fenêtre pendant laquelle aucun ordre de réduction ne peut partir.
7. Lever le halt : `okxq control resume --config ... --reason rotation_terminee --actor <nom>` (préconditions
   réelles : `resume_stability_seconds`, 300 s par défaut).

*Lacune* : il n'existe **aucune commande de vérification d'identifiants pour LIVE sans envoi**.
`okxq live preflight` vérifie la garde LIVE (manifeste signé, gates, commit, hash de configuration), pas la
validité de la clé. La vérification d'une clé LIVE repose donc sur `demo preflight` avec les clés DEMO et
sur des lectures d'état privées au démarrage du gateway.

---

## B. Clé TypeSafe / JEV (`jev-worker.env` uniquement)

**Pourquoi aucun halt n'est nécessaire** : JEV est hors du chemin critique. Une clé refusée dégrade les
décisions en `JEV_FALLBACK` ; elle ne menace ni les positions ni les protections
(`docs/runbooks/jev_outage.md`). Le worker ne reçoit d'ailleurs ni clé d'échange, ni clé opérateur, ni
position, ni équité — et ne monte ni `/app/data` ni `/app/artifacts`.

1. Créer la nouvelle clé chez le fournisseur.
2. Poser `TYPESAFE_API_KEY` dans `/opt/okxq/env/jev-worker.env` (600), et nulle part ailleurs.
3. `docker compose up -d jev-worker`.
4. Vérifier :
   ```bash
   .venv/bin/okxq jev status            # worker, available, reasons, real_call
   .venv/bin/okxq jev validate-fixtures # contrat request/response — ne teste PAS la clé
   ```
   et `GET /api/v1/jev/status`. Hors ligne, `real_call` vaut `NOT_RUN` : un appel non exécuté n'est jamais
   présenté comme réussi. Un `401` dans `jev_error_total` signifie que la clé est refusée.
5. Révoquer l'ancienne clé après confirmation.

---

## C. Secret opérateur `OPERATOR_AUTH_SECRET` (`api.env` uniquement)

**Trois conséquences, toutes immédiates. À lire avant de taper quoi que ce soit.**

1. **Toutes les clés de rôle dérivées sont invalidées.** Les clés présentées à l'interface ne sont pas le
   secret : ce sont des dérivations `HMAC-SHA256(secret, "okxq-ui-key:v1:<rôle>")` pour `reader`,
   `operator` et `admin` (`derive_role_keys()` dans `src/okxq/api/auth.py`). Changer le secret change les
   trois clés. **Il faut donc les regénérer et les redistribuer.**
2. **Toutes les sessions en cours sont invalidées.** Le cookie `okxq_session` est une session signée HMAC
   avec le même secret (`SessionSigner`) : aucune session existante ne vérifie plus. Personne ne reste
   connecté, sur aucun navigateur.
3. **Un manifeste d'approbation LIVE signé cesse d'être vérifiable.** Le manifeste est signé
   HMAC-SHA256 par `OPERATOR_AUTH_SECRET` (`src/okxq/config/live_guard.py`) : après rotation, la signature
   ne vérifie plus et LIVE est refusé. Il faut le **resigner** avec
   `scripts/sign_approval_manifest.py`, ce qui est une décision d'approbation, pas une formalité (§71.3).

Procédure :

1. Générer sur le serveur, sans afficher : `openssl rand -hex 32` (32 octets), redirigé directement dans le
   fichier, ou via `deploy/install.sh` avec `OKXQ_OPERATOR_KEY_POSE` posé dans l'environnement.
2. Poser `OPERATOR_AUTH_SECRET` dans `/opt/okxq/env/api.env`, et nulle part ailleurs. Le `gateway`, le
   `risk`, la `strategy`, le `collector` et le `jev-worker` ne le reçoivent pas : l'interface **demande**
   des actions, elle n'en signe aucune chez OKX. Un vol de la clé opérateur donne l'accès à la console, pas
   la capacité de signer un ordre.
3. `docker compose up -d api`. Sans secret, l'API **refuse de démarrer** (« une API de pilotage joignable
   sans clé n'est pas une commodité »).
4. Regénérer les clés de rôle, à l'intérieur du conteneur, dans son `/tmp` (la racine de l'image est en
   lecture seule, `/app/artifacts` est monté en lecture seule pour l'`api`, et `/tmp` est un tmpfs) :
   ```bash
   docker compose exec -T api okxq api keys --out /tmp/api_keys.txt
   docker compose exec -T api cat /tmp/api_keys.txt      # à ne faire que sur un canal maîtrisé
   docker compose exec -T api rm -f /tmp/api_keys.txt
   ```
   La commande n'imprime que le chemin, les permissions et une **empreinte tronquée** par rôle : elle
   permet de vérifier qu'on parle de la même clé sans la divulguer. `--montrer` existe et écrit les clés
   sur la sortie standard : à éviter, pour la raison rappelée en tête de ce runbook.
5. Distribuer chaque clé de rôle **hors bande**, une par destinataire, selon le moindre privilège :
   `reader` pour consulter, `operator` pour demander une action, `admin` en dernier recours.
6. Vérifier : `GET /` sans clé doit rendre 401 ou 403 ; une ancienne clé doit être refusée et laisser un
   événement d'audit `DENIED` (`GET /api/v1/audit/operator-actions`, `okxq risk status --actions 10`) ;
   `GET /api/v1/system/status` avec la nouvelle clé `reader` doit répondre.
7. Si LIVE est concerné : resigner le manifeste, puis `okxq live preflight --config <profil>` avant tout
   autre geste.

---

## D. Mot de passe PostgreSQL

**La particularité** : c'est le seul secret **légitimement présent dans plusieurs fichiers**. Il figure dans
`POSTGRES_PASSWORD` (`env/postgres.env`) **et** dans le `DATABASE_URL` de chaque service qui lit la base —
`api`, `collector`, `strategy`, `risk`, `gateway`, `jev-worker`, et `migrate` s'il a son fichier. Une
rotation les touche tous en même temps ; en oublier un laisse un rôle qui ne démarre plus.

**Le piège** : `POSTGRES_PASSWORD` n'est lu qu'à l'**initialisation** d'un nouveau volume `pgdata`. Sur un
cluster existant, changer la variable ne change **rien** au mot de passe du rôle : il faut l'altérer dans
la base.

1. Arrêter les services applicatifs (garder `postgres`) :
   `docker compose stop api jev-worker strategy collector gateway risk` — voir
   `docs/runbooks/shutdown.md` pour l'ordre et la politique de positions si des positions sont ouvertes.
2. Changer le mot de passe du rôle **sans le mettre sur la ligne de commande** (il serait visible dans la
   liste des processus et dans l'historique de `psql`) :
   ```bash
   docker compose exec -it postgres psql -U okxq -d okxq_paper
   # puis, dans psql :   \password okxq
   ```
3. Mettre à jour `POSTGRES_PASSWORD` dans `/opt/okxq/env/postgres.env` (pour qu'une réinitialisation future soit
   cohérente) **et** le `DATABASE_URL` de chaque service concerné. `deploy/install.sh` sait le faire pour
   tous les fichiers à la fois quand le mot de passe est déjà posé dans `/opt/okxq/env/postgres.env`.
4. Redémarrer : `docker compose up -d`.
5. Vérifier — et **ne pas se fier aux healthchecks applicatifs** : ils ouvrent une connexion TCP vers
   `postgres:5432` et ne testent pas l'authentification ; ils resteraient verts avec un mot de passe faux.
   Contrôles réels :
   ```bash
   docker compose logs --tail=30 risk gateway        # une erreur d'authentification apparaît ici
   docker compose run --rm migrate okxq db current   # ouvre une VRAIE connexion authentifiée
   curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8899/health/ready
   ```

**`OKXQ_BACKUP_PASSPHRASE` vit dans le même fichier (`/opt/okxq/env/postgres.env`), et sa rotation a une conséquence propre** : les
archives déjà chiffrées restent chiffrées avec **l'ancienne** phrase. Conserver l'ancienne phrase, hors du
serveur, aussi longtemps que des archives chiffrées avec elle sont retenues (`--keep`, 14 par défaut) —
sinon ces sauvegardes deviennent irrécupérables. Après rotation, refaire une sauvegarde **et** un essai de
restauration (`docs/runbooks/backup_restore.md`).

---

## Vérifications communes

```bash
python scripts/security_check.py --json      # "ok": true, "anomalies": []
bash deploy/etat.sh                          # n'imprime que les NOMS des variables, jamais les valeurs
```

- `security_check.py` refuse : un secret committé, un `*.env` suivi par git, une sauvegarde versionnée,
  une configuration activant LIVE, et un service de `compose.yaml` recevant un secret qui ne lui appartient
  pas. Une valeur qui s'annonce fausse (`remplacer-par-…`) n'est pas un secret ; aucun chemin n'est pour
  autant sur liste blanche.
- `deploy/etat.sh` liste les noms de variables de `api.env`, `gateway.env` et `jev-worker.env` : c'est le
  bon outil pour constater qu'une clé est **présente** sans la lire.
- Les journaux masquent les secrets : `OPERATOR_AUTH_SECRET` fait partie des champs rédigés
  (`src/okxq/runtime/logging.py`). Ne jamais désactiver ce masquage pour « déboguer ».
- `okxq config validate` refuse une configuration YAML dont une clé ressemble à un secret et porte une
  valeur non vide : un secret n'a rien à faire dans `configs/`.

## Cas d'échec

| Symptôme | Cause probable | Geste |
|---|---|---|
| un rôle refuse de démarrer avec `identifiants d'échange présents dans un rôle qui ne doit jamais les recevoir` | clé OKX posée dans le mauvais `*.env` | la retirer ; c'est la régression que ce runbook existe pour éviter |
| `OPERATOR_AUTH_SECRET absent : …` | secret non posé dans `api.env` | poser le secret ; l'API ne démarre pas sans lui, exprès |
| 401/403 sur toute l'interface après rotation | clés de rôle non regénérées | étape C.4, puis redistribution hors bande |
| `signature du manifeste invalide` / LIVE refusé | secret opérateur tourné | resigner le manifeste (§71.3) |
| erreurs d'authentification base sur un seul service | un `DATABASE_URL` oublié | étape D.3 |
| `archive chiffrée et aucune phrase de passe disponible` | phrase de sauvegarde tournée trop tôt | restaurer avec l'ancienne phrase ; l'archive est sinon perdue |
| `jev_error_total` en hausse, `401` | clé TypeSafe refusée | étape B ; le circuit se referme seul après le cooldown |

## Ce qui reste à faire par un humain

- **Créer et révoquer** les clés chez OKX et chez TypeSafe : aucune commande de la plateforme ne le fait.
- **Restreindre la clé OKX côté fournisseur** (pas de retrait, liste d'IP) — c'est la seule protection qui
  survit à une compromission du serveur.
- **Distribuer les clés de rôle** hors bande, au moindre privilège, et retirer l'accès des partants.
- **Resigner le manifeste LIVE** après rotation du secret opérateur, ce qui suppose de reconstituer les
  preuves des trois gates (§71).
- **Décider de la rétention de l'ancienne phrase de sauvegarde** et la ranger hors du serveur.
- **Consigner la date, l'acteur et le motif** de chaque rotation. Les actions opérateur du Risk Engine sont
  auditées en base ; la rotation d'un secret, elle, ne laisse aucune trace applicative.
- **Lacune à connaître** : aucune rotation ni révocation réelle n'a jamais été exercée sur ce déploiement
  (`docs/threat_model.md`, M1 : `NOT_RUN`). Il n'existe pas de commande `okxq` de rotation, de révocation ni
  de test d'identifiants : tout ce runbook est une procédure manuelle, et §60 exige que rotation, révocation
  et reprise soient **testées** avant LIVE. Le premier essai réel doit être fait sur des clés DEMO.
