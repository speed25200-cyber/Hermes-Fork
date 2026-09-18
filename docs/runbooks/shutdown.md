# Runbook — arrêt propre et arrêt d'urgence (§62)

**Objectif** : arrêter la plateforme sans produire d'ordre dont l'état soit inconnu, et sans laisser
croire que l'arrêt a fermé quoi que ce soit. Le choix de conserver ou de fermer les positions est une
**politique explicite**, jamais un effet secondaire de `docker compose down`.

**Ce que fait un signal** : `SIGINT`/`SIGTERM` positionnent un unique drapeau
(`_install_signal_handlers()` dans `src/okxq/runtime/composition.py` : `loop.add_signal_handler(sig,
stop.set)`). Le signal **demande** un arrêt ; il n'interrompt pas une décision en cours. Le
`DecisionScheduler` sort de sa boucle puis attend l'inactivité (`await self.wait_idle()`), et seulement
ensuite `Runtime.aclose()` ferme les composants **dans l'ordre inverse de l'assemblage**, une erreur
d'arrêt ne masquant pas les suivantes.

**POURQUOI cette précaution** — c'est le cœur de ce runbook. Couper au milieu d'un envoi laisserait un
ordre dont on ne connaît pas l'état : envoyé ou non, accepté ou non, partiellement exécuté ou non. Le
système devrait alors le marquer UNKNOWN, réserver une exposition pessimiste et bloquer les intentions
conflictuelles jusqu'à rapprochement (§52.1, `docs/runbooks/unknown_order.md`). C'est exactement la
situation que la réconciliation doit éviter d'avoir à traiter — non parce qu'elle en est incapable, mais
parce qu'un UNKNOWN évitable est un risque gratuit, créé par nous, sur de l'argent réel. D'où les délais
de grâce de `compose.yaml` : **120 s pour le `gateway`** (le signataire), 30 s pour les autres rôles,
20 s pour l'`api`, 60 s pour `postgres`. Couper le signataire en 10 s annulerait cette garantie.

## Préconditions

- Savoir quelle **politique de positions** s'applique. `ShutdownSequence` n'accepte que deux valeurs, et
  aucune par défaut : `keep_positions_supervised` ou `flatten_before_stop`
  (`src/okxq/runtime/startup.py`). Le choix doit être fait avant de taper la première commande.
- Savoir s'il reste des ordres ouverts et des positions : `okxq risk status`,
  `GET /api/v1/orders`, `GET /api/v1/portfolio/positions`.
- Un accès à l'application OKX ou à l'API en lecture : après l'arrêt, c'est la seule autorité.

## Procédure — arrêt propre

1. **Suspendre les entrées d'abord.** Rien d'autre n'a de sens avant : vider une file d'intentions
   pendant que la boucle en crée de nouvelles ne termine jamais.
   ```bash
   .venv/bin/okxq control pause --config configs/<mode>.yaml \
     --reason arret_planifie --actor <nom>
   ```
   SOFT_HALT : aucune augmentation d'exposition ; réductions et protections restent autorisées. L'action
   est auditée (`operator_actions`) et **persistée** : elle survivra à l'arrêt, et le prochain démarrage
   la retrouvera à l'étape `authorize_entries` (voir `docs/runbooks/startup.md`).

2. **Laisser la décision en cours se terminer.** Ne rien envoyer de plus. Attendre la fin de la minute
   courante ; avec `decision_overlap_policy: skip` (valeur de `configs/base.yaml`), aucune décision ne
   se superpose à la suivante.

3. **Traiter les intentions en attente et confirmer l'absence d'incertitude.**
   ```bash
   .venv/bin/okxq risk status --config configs/<mode>.yaml --actions 10
   curl -s 'http://127.0.0.1:8899/api/v1/orders?state=UNKNOWN&key=<clé_reader>'
   ```
   Un UNKNOWN non résolu **interdit** de considérer l'arrêt propre : le traiter d'abord
   (`docs/runbooks/unknown_order.md`). S'arrêter sur un UNKNOWN revient à mettre en pause l'enquête sur
   un ordre peut-être vivant.

4. **Appliquer la politique de positions, explicitement.**
   - `keep_positions_supervised` — les positions restent ouvertes chez OKX. Vérifier que les protections
     sont bien en place côté échange **avant** d'arrêter, et que quelqu'un sait qu'elles ne seront plus
     surveillées.
   - `flatten_before_stop` — sortir d'abord, puis arrêter :
     `okxq control request-flatten --config ... --reason arret_avec_sortie --actor <nom>`, et suivre
     `docs/runbooks/emergency_flatten.md` jusqu'à ce que les résidus soient nuls et réconciliés. L'écran
     ne montre jamais « FLAT » sans preuve de réconciliation.
   - Pour annuler les ordres d'entrée ouverts sans sortir des positions :
     `okxq control cancel-entry-orders --config ... --reason arret_planifie --actor <nom>` (passe en
     HARD_HALT ; la demande est consommée par le gateway, donc **le gateway doit encore tourner**).

5. **Enregistrer un checkpoint.** Un arrêt sans trace de l'état au moment de l'arrêt rend la reprise
   aveugle.
   ```bash
   .venv/bin/okxq reports daily --config configs/<mode>.yaml \
     --out reports/arret-$(date -u +%Y%m%dT%H%M%SZ).json
   infra/backup.sh --dir /opt/okxq          # base + configurations + modèles (voir backup_restore.md)
   ```
   La sauvegarde exige que le service `postgres` tourne encore : la faire **avant** d'arrêter la base.

6. **Arrêter les services, non critiques d'abord.**
   ```bash
   cd /opt/okxq
   docker compose stop api jev-worker strategy collector   # 20 s / 30 s de grâce
   docker compose stop gateway                             # 120 s de grâce : le signataire en dernier
   docker compose stop postgres                            # 60 s
   ```
   `docker compose stop` envoie `SIGTERM` et respecte `stop_grace_period` ; `init: true` garantit un vrai
   PID 1 qui transmet le signal. Préférer `stop` à `down` : `down` supprime les conteneurs (et, avec
   `-v`, les volumes — donc le journal financier).

## Arrêt d'urgence

L'urgence ne dispense pas de l'ordre, elle le raccourcit. **Faire persister l'intention d'arrêt avant de
couper** : un halt écrit en base survit au processus, une décision prise dans la tête de l'opérateur non.

1. `okxq control cancel-entry-orders --config ... --reason urgence_<motif> --actor <nom>` (HARD_HALT,
   immédiat, audité) — ou `request-flatten` si l'urgence exige de sortir.
2. Laisser au `gateway` le temps de consommer la demande et de confirmer ; c'est la raison de ses 120 s.
3. Seulement si le processus ne répond plus : `docker compose kill gateway`. **Conséquence assumée** :
   toute tentative envoyée sans résultat persisté deviendra UNKNOWN au prochain démarrage
   (`gateway.startup_check()`), avec réservation pessimiste et blocage des intentions conflictuelles.
   C'est le prix du `kill`, et il doit être un choix, pas une surprise.
4. Incident de sécurité ou état de compte inconnu prolongé : `docs/runbooks/emergency_flatten.md`, et
   intervenir directement sur OKX si nécessaire — l'application OKX reste l'autorité.

## Ce qui reste exposé après l'arrêt

**Les positions et les ordres ouverts NE DISPARAISSENT PAS avec le processus.** Ils vivent chez OKX. Un
moteur arrêté n'a rien fermé : il a seulement cessé de regarder. `deploy/preflight.sh` est bâti sur ce
constat, et refuse d'écrire « 0 position » quand il n'a pas pu lire — « je n'ai pas réussi à savoir » ne
doit jamais devenir « il n'y a rien ».

Restent donc actifs, sans supervision : positions ouvertes, ordres ouverts (y compris d'entrée si
l'étape 4 n'a pas été faite), protections déposées côté échange (elles s'appliquent toujours, mais plus
personne ne les replace si elles sont consommées), funding qui continue de courir, et liquidation
possible. Ne restent **pas** actifs : la boucle de décision, la réconciliation, les kill switches
dynamiques, les alertes.

## Vérifications avant de déclarer l'arrêt terminé

1. `docker compose ps` : tous les services visés en `exited`, code de sortie 0.
2. Aucun ordre en `SUBMITTED` ni `UNKNOWN` au moment de l'arrêt (relevé de l'étape 3, conservé).
3. Le halt attendu est **persisté** : relire après l'arrêt de la base n'est plus possible, donc conserver
   la sortie de `okxq risk status` de l'étape 3 comme preuve, ou la relire au prochain démarrage.
4. Le checkpoint (`reports/arret-*.json`) et la sauvegarde existent et sont lisibles.
5. **Écrire noir sur blanc l'exposition résiduelle** : instruments, sens, tailles, protections en place,
   et qui en est responsable pendant l'arrêt. Un arrêt sans ce relevé n'est pas terminé.

## Ce qui reste à faire par un humain

- **Choisir la politique de positions** et l'assumer. Le code refuse de la deviner
  (`ShutdownSequence` rejette toute autre valeur que les deux prévues).
- **Surveiller les positions conservées** pendant l'arrêt, depuis l'application OKX.
- **Décider le moment de la reprise**, en sachant que `resume` a des préconditions réelles
  (`resume_stability_seconds`, rôle autorisé) et que HARD_HALT / EMERGENCY_FLATTEN ne se lèvent jamais
  automatiquement.
- **Lacune à connaître** : `ShutdownSequence` et sa constante `SHUTDOWN_ORDER` (`suspend_entries`,
  `drain_pending_intents`, `confirm_cancellations_and_protections`, `write_checkpoint`,
  `stop_non_critical`) sont implémentées et testées (`tests/unit/test_startup_supervisor.py`) mais **ne
  sont câblées par aucun processus** : `run_process()` n'appelle que le drainage du planificateur puis
  `Runtime.aclose()`. Les cinq étapes de §62 doivent donc être exécutées **à la main**, dans l'ordre
  ci-dessus. Il n'existe pas non plus de commande `okxq control shutdown` ni `okxq control drain` : ne
  pas en chercher une.
