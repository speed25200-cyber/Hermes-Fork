# ADR-002 — Persistance : SQLAlchemy 2 + Alembic sur PostgreSQL, SQLite mémoire pour les tests hermétiques

**Statut** : accepté (18 septembre 2026).

**Contexte** : §6 et §44 imposent PostgreSQL avec TIMESTAMPTZ, NUMERIC de précision explicite,
contraintes et migrations testées ; §64/§67 imposent des tests hermétiques sans service externe.

**Décision** : un seul schéma (`okxq.persistence.models`) écrit en types portables (`TZDateTime`,
`Numeric(28,8)`/`Numeric(28,12)`, `JSON` avec variante `JSONB` sur PostgreSQL). Les tests unitaires
utilisent `sqlite+pysqlite:///:memory:` (clés étrangères activées) ; les tests de migrations, de
contraintes réelles, d'outbox et de baux tournent sur PostgreSQL et sont marqués `integration`
(`OKXQ_TEST_DATABASE_URL`). Sans base, ils sont `NOT_RUN`, jamais `PASS`.

**Conséquences** : les comportements propres à PostgreSQL (verrous `FOR UPDATE SKIP LOCKED`, `JSONB`)
sont encapsulés dans les dépôts et testés en intégration ; SQLite ne sert jamais en exploitation.
