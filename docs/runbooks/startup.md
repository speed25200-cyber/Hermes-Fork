# Runbook — démarrage opérationnel (§52.4)

**Objectif** : amener un déploiement de l'état « arrêté » à l'état « entrées autorisées », dans un ordre
qui rend impossible l'envoi d'un ordre d'entrée avant que le leadership, la réconciliation, les
protections et les données soient établis. La séquence est implémentée par
`build_startup_sequence()` dans `src/okxq/runtime/composition.py` et exécutée par `StartupSequence.run()`
(`src/okxq/runtime/startup.py`, constante `STEP_ORDER`). Elle n'est pas une convention de documentation :
un échec bloquant **arrête la séquence là** et `entries_authorized` reste faux.

**Deux points à ne jamais perdre de vue**

- `authorize_entries` est la **DERNIÈRE** étape. Aucune intention d'entrée ne peut partir avant elle.
  Le processus continue néanmoins de tourner sans elle : observer, réduire et rapporter restent utiles
  précisément quand les entrées sont interdites (alerte P1 `entries_not_authorized`). Un démarrage
  incomplet n'est pas un arrêt silencieux.
- **Un halt persisté n'est JAMAIS remis à zéro par un démarrage.** Le `KillSwitch` relit son état depuis
  la base (`SqlRiskStateStore`) à la construction ; `authorize_entries` échoue si le niveau n'est pas
  `NONE`. Les pertes journalières et le high-water mark sont dans la même table. Redémarrer n'est pas
  une façon de lever une protection : seule `okxq control resume` le fait, sous préconditions.

## Préconditions

1. **Le schéma doit être migré AVANT le démarrage.** `build_runtime()` appelle `assert_schema_ready()`
   avant tout composant qui lit la base, et refuse de démarrer sur un schéma absent ou incomplet avec un
   message qui donne la commande exacte. Le runtime ne crée jamais le schéma à la volée : un schéma créé
   par le runtime diverge silencieusement de celui des migrations.

   ```bash
   # Une base doit être DÉSIGNÉE : aucune valeur par défaut n'est appliquée.
   export OKXQ_DATABASE_URL='postgresql+psycopg://okxq:...@127.0.0.1:5432/okxq_paper'
   .venv/bin/okxq db upgrade                      # --revision head par défaut
   .venv/bin/okxq db current                      # révision appliquée, révisions en attente
   .venv/bin/okxq db check                        # code 1 si le schéma diverge des modèles
   ```
   En déploiement, c'est le service `migrate` de `compose.yaml` qui fait ce travail ; les rôles
   écrivains l'attendent (`condition: service_completed_successfully`). Si la migration échoue, ils ne
   démarrent pas — comportement voulu, pas panne à contourner.

2. Configuration valide : `okxq config validate --config configs/<mode>.yaml` (schéma strict, absence de
   secrets dans le YAML).
3. Environnement sain : `okxq doctor --config configs/<mode>.yaml` (hors ligne par défaut).
4. Un fichier d'environnement **par service** posé dans `infra/env/` (modèles `*.env.example`) ou
   `/opt/okxq/env/` sur le serveur. Les clés OKX ne vivent que dans `gateway.env` (voir
   `docs/runbooks/key_rotation.md`).
5. LIVE : `live_enabled` faux refuse le démarrage (`assert_mode_allows_process`), et `okxq live run`
   exige le manifeste d'approbation signé (§71). Il n'existe aucune option `--force`.

## Procédure — les huit étapes, dans l'ordre

Démarrage d'un rôle : `okxq <paper|shadow|demo|live> run --config configs/<mode>.yaml --role <rôle>`.
En déploiement : `docker compose up -d` (l'ordre entre services est porté par `depends_on`).

1. **`load_config`** — la configuration chargée est celle attendue : mode et périmètre de compte sont
   rappelés dans le détail de l'étape. *Échec* : ne se produit pas ici ; un profil invalide échoue plus
   tôt, au chargement. Rejouer `okxq config validate`.

2. **`verify_environment`** — rejoue `assert_credentials_separation(cfg, role)` : un rôle autre que
   `gateway` (ou `all`) auquel on a injecté `OKX_API_KEY`, `OKX_API_SECRET` ou `OKX_API_PASSPHRASE`
   est refusé. *Pourquoi* : un secret présent dans l'environnement d'un processus est lisible par tout
   ce qui y tourne, dépendance compromise incluse. *Échec (bloquant)* : le déploiement est mal
   configuré. Retirer les variables du mauvais fichier, relancer `python scripts/security_check.py`,
   puis redémarrer. Ne jamais contourner en passant le rôle à `all`.

3. **`acquire_leadership`** — un seul gateway par compte peut signer à un instant donné (§52.3). Pour les
   rôles sans chemin d'envoi, l'étape réussit en non bloquant (« pas d'écrivain d'exécution dans ce
   rôle »). Le bail lui-même est tenu par le gateway (`src/okxq/execution/leadership.py`, table
   `runtime_leases` : titulaire, expiration, `fencing_token`). *Vérification* :
   `GET /api/v1/system/status` → `leadership.execution_writer.active`. *Échec* : si l'autorité d'envoi
   ne peut pas être confirmée, aucune augmentation de risque n'est permise — laisser le halt en place
   et chercher le processus concurrent avant de relancer.

4. **`connect_private_streams`** — hors DEMO/LIVE, il n'y a pas de flux privé à ouvrir : l'étape réussit
   en non bloquant et le composant `private_stream` est marqué OK avec ce motif. En DEMO/LIVE, les flux
   sont ouverts par le gateway. *Échec* : traiter comme une panne de connectivité privée ; les entrées
   restent interdites tant que l'état du compte n'est pas observable.

5. **`bootstrap_reconcile`** — appelle `gateway.startup_check()` : **toute tentative envoyée sans
   résultat persisté devient UNKNOWN** (état `SUBMITTED` avec `attempt_count > 0`), la réconciliation
   est marquée obligatoire, et l'étape rapporte le nombre d'ordres à rapprocher. *Pourquoi* : un crash
   entre l'envoi et la journalisation de l'ACK laisse un ordre dont l'état est inconnu ; le présumer
   absent serait la pire hypothèse possible. *Échec ou compte non nul* : suivre
   `docs/runbooks/unknown_order.md`. Une exposition pessimiste est réservée et les intentions
   conflictuelles sont bloquées tant que l'état n'est pas final.

6. **`restore_protections`** — les protections vivent côté échange ; hors DEMO/LIVE l'étape est sans
   objet, et en DEMO/LIVE elles sont restaurées par le gateway. *Échec* : ne jamais autoriser des entrées
   sur des positions non protégées ; passer en SOFT_HALT (`okxq control pause`) et replacer les
   protections avant toute reprise (§54).

7. **`validate_data`** — **bloquante** : si aucune donnée de marché n'a encore été reçue
   (`market.last_available_at is None`), le composant `market_data` passe en FAULT et les entrées ne
   sont pas autorisées. La collecte est démarrée *avant* la séquence, précisément pour que cette étape
   puisse constater des données. *Échec* : vérifier le collecteur (`docker compose logs collector`),
   `GET /api/v1/data/quality`, et `docs/runbooks/book_resync.md`. Un marché immobile n'est pas une panne ;
   une absence de réception en est une.

8. **`authorize_entries`** — **bloquante et dernière** : échoue si `kill_switch.level != NONE`, avec le
   détail « halt persisté au démarrage : `<niveau>` ». *Échec* : ce n'est pas un incident de démarrage,
   c'est une protection qui fait son travail. Lire le motif (`okxq risk status`), traiter la cause, puis
   `okxq control resume --config ... --reason <motif> --actor <nom>`. Les préconditions sont réelles :
   `resume_stability_seconds` (300 s par défaut) doit s'être écoulé, et
   `auto_resume_after_critical_halt` est faux par construction — HARD_HALT et EMERGENCY_FLATTEN ne se
   lèvent que par une action opérateur tracée.

## Vérifications avant de considérer le démarrage terminé

```bash
.venv/bin/okxq db check                                       # schéma aligné (code 0)
.venv/bin/okxq risk status --config configs/<mode>.yaml       # halt_level attendu, limites versionnées
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8899/health/live    # 200
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8899/health/ready   # 200 quand tout est prêt
```

- `/health/ready` répond **503 tant qu'un composant requis n'est pas prêt** : c'est normal au premier
  démarrage (réconciliation et données). `/health/live` ne dit que « le processus répond » ; ne pas
  confondre les deux.
- `GET /api/v1/system/status` (rôle `reader`) donne en un seul document : mode, `live_enabled`, versions
  et `config_hash`, `leadership.execution_writer`, `halts`, `components`, `ready`,
  `readiness_reasons`, `alerts_active`.
- Sur le serveur, `deploy/etat.sh` imprime conteneurs, santé, présence (jamais valeur) des secrets,
  statut système et journaux.
- L'accès à `/` sans clé doit renvoyer 401 ou 403. Une console ouverte est un défaut, pas une commodité.

## Cas d'échec fréquents

| Symptôme | Cause | Geste |
|---|---|---|
| `schéma de base absent ou incomplet` | migrations non appliquées | `okxq db upgrade` avec une base désignée, puis relancer |
| `aucune base désignée` | ni `OKXQ_DATABASE_URL`, ni `DATABASE_URL`, ni `OKXQ_SQLITE_PATH` | poser l'URL ; aucun défaut n'est appliqué (§44) |
| `identifiants d'échange présents dans un rôle…` | secret dans le mauvais `*.env` | un fichier par service ; `scripts/security_check.py` |
| `mode LIVE demandé alors que live_enabled est faux` | profil non autorisé | §71, manifeste signé ; pas de contournement |
| `entrees_non_autorisees` (P1) | une étape bloquante a échoué | lire le rapport de démarrage dans les journaux du rôle |
| `/health/ready` reste 503 | données ou réconciliation en attente | `readiness_reasons` dans `/api/v1/system/status` |

## Ce qui reste à faire par un humain

- **Décider** si un halt persisté doit être levé, et le justifier. Le système ne le fera pas.
- **Lire le rapport de démarrage** : il est renvoyé par `run_process()` dans le compte rendu du
  processus (clé `startup`, avec le détail et la durée de chaque étape) et tracé dans les journaux JSON.
  *Lacune* : il n'existe **aucune commande `okxq` qui affiche le dernier rapport de démarrage** ; il faut
  le lire dans les journaux du rôle (`docker compose logs <service>`).
- **DEMO/LIVE** : exécuter le preflight connecté avant d'ouvrir les entrées —
  `okxq demo preflight --config configs/demo.yaml` (clés DEMO, profil, en-tête `x-simulated-trading`,
  mode de compte, sans envoyer d'ordre) ou `okxq live preflight --config configs/live.disabled.yaml`
  (manifeste, gates, commit, hash de configuration). Vérifier positions initiales, ordres, marge et
  protections avant toute exposition (§71.4).
- **Défaut connu à signaler** : le service `migrate` de `compose.yaml` lance
  `["okxq", "db", "upgrade", "head"]`. `head` y est un argument **positionnel**, que la commande
  n'accepte pas : elle échoue avec « Got unexpected extra argument(s) (head) ». La forme correcte est
  `okxq db upgrade` (défaut `head`) ou `okxq db upgrade --revision head`. Tant que ce n'est pas corrigé,
  la migration en déploiement doit être lancée à la main.
