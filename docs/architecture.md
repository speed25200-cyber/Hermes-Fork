# Architecture

## Vue d'ensemble

```
             ┌──────────────────────── processus distincts (compose.yaml) ────────────────────────┐
             │                                                                                     │
  OKX WS/REST public ──► collector ──► normalisation ──► validateurs de carnet ──► archive Parquet │
             │                              │                                          │           │
             │                              ▼                                          ▼           │
   sources JEV ──► jev-worker ──► cache sémantique (PostgreSQL) ──► features JEV (âge, dispo)      │
             │                                                                                     │
             │   strategy : toutes les 60 s UTC                                                    │
             │   snapshot PIT ─► features ─► modèle promu ─► edges (coûts, incertitude)            │
             │   ─► optimiseur (CVXPY) ─► arrondis/deltas ─► OrderIntent ─────────────┐            │
             │                                                                        ▼            │
             │   risk : RiskEngine indépendant ─► RiskDecision liée au hash ─► réservations        │
             │                                                                        │            │
             │   gateway (UNIQUE writer, bail + fencing) : outbox ─► recheck ─► envoi ─► ACK/UNKNOWN│
             │        ▲                                                               │            │
             │        │ ExchangeAdapter (OKX privé | VirtualExchange)   fills/events ◄─┘            │
             │        │                                                                             │
             │   accounting : ledger append-only ◄── fills dédupliqués ; réconciliation périodique  │
             │                                                                                     │
             │   api : FastAPI + interface Hermes conservée (lecture) + actions opérateur auditées  │
             └─────────────────────────────────────────────────────────────────────────────────────┘
```

## Frontières de confiance

| Composant | Secrets reçus | Peut envoyer un ordre | Notes |
|---|---|---|---|
| collector | aucun | non | endpoints publics seulement |
| jev-worker | `TYPESAFE_API_KEY` | non | ne voit ni positions, ni patrimoine, ni clés OKX |
| strategy | aucun | non | produit des propositions (`OrderIntent`) |
| risk | aucun | non | autorité finale : ALLOW/REDUCE/REJECT/FLATTEN |
| gateway | `OKX_API_*` | **oui, seul** | bail durable, fencing, allowlist d'endpoints, recheck à l'envoi |
| api | `OPERATOR_AUTH_SECRET` | non | actions opérateur → `operator_actions` (REQUESTED), exécutées par le superviseur |
| postgres | — | — | non exposé publiquement |

Le modèle ne peut pas contourner le risque : le seul chemin vers `ExchangeAdapter.place_order` passe par
`ApprovedOrder` (validé structurellement : hash du payload = hash approuvé) puis par le gateway, qui
revérifie approbation, contexte, mode et état d'urgence avant d'envoyer une fois.

## Modes

| Mode | Données | Ordres | Clés OKX | Usage |
|---|---|---|---|---|
| RESEARCH | archives | simulateur | non | expériences, backtests |
| PAPER | publiques temps réel | simulateur local | non | défaut |
| SHADOW | publiques temps réel | aucun (décisions archivées avant résultat) | non | forward test |
| DEMO | publiques + privées démo | OKX démo (`x-simulated-trading: 1`) | démo | validation technique |
| LIVE | publiques + privées | OKX réel | réelles | **désactivé** ; manifeste signé requis |

## Causalité

`feature.available_at ≤ snapshot.cutoff_at ≤ decision.started_at` ; `forecast.snapshot_id` = snapshot
courant ; `approval.created_at ≤ order.sent_at ≤ approval.expires_at`. Le replay ordonne par
`(available_at, ingest_seq)`. Un document JEV devient utilisable à `features_committed_at`, jamais à sa
date déclarée de publication.

## Persistance

PostgreSQL (30 tables, `okxq.persistence.models`) : versions d'instruments, univers, partitions brutes,
documents et résultats JEV, schémas de features, versions de modèles, expériences, décisions, cibles,
intentions, approbations, ordres, événements, fills, réservations, ledger, snapshots, événements de
risque, état de risque (halts, pertes journalières, HWM), baux, outbox, offsets, actions opérateur.
Parquet immuable pour les données brutes volumineuses, référencé par manifeste et checksum.

## Boucle décisionnelle (§55)

`runtime.scheduler.DecisionScheduler` déclenche `runtime.decision_loop.DecisionLoop.run_once(cutoff,
deadline)` à chaque frontière : état opérationnel → snapshot → features → inférence → edges →
optimiseur → arrondis/deltas → risque → gateway → décision persistée (TRADE / NO_TRADE / SKIPPED /
FAILED) avec raisons et alternatives rejetées. Une minute manquée n'est jamais rattrapée en rafale.

## Démarrage et arrêt (§52.4, §62)

`runtime.startup.StartupSequence` : configs → environnement → leadership → flux privés → réconciliation
→ protections → validation des données → autorisation des entrées. `ShutdownSequence` : suspendre les
entrées → drainer → confirmer annulations/protections → checkpoint → arrêter le non-critique ; la
politique de positions est explicite.
