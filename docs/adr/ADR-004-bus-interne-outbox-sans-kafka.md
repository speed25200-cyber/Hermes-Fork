# ADR-004 — Bus interne : outbox transactionnelle et bail durable, pas de Kafka

**Statut** : accepté (18 septembre 2026).

**Contexte** : §41 autorise un bus mémoire borné intra-processus mais exige journal durable et
transactional outbox pour intentions/ordres/fills ; §52 exige un seul writer avec bail, heartbeat et
fencing, et une idempotence locale (livraison « au moins une fois »).

**Décision** : les événements financiers sont écrits dans PostgreSQL (`outbox_events`) dans la même
transaction que l'écriture métier ; le gateway unique les réclame avec un bail (`runtime_leases`,
token de fencing monotone) et marque tentative + payload exact avant l'envoi réseau ; la consommation
est idempotente (`consumer_offsets`, clés d'idempotence). Les flux de marché publics passent par des
files asyncio bornées intra-processus avec compteurs de pertes.

**Conséquences** : aucune garantie « exactly once » à travers le réseau n'est annoncée ; un crash
entre envoi et journalisation produit `UNKNOWN`, réconcilié avant tout renvoi.
