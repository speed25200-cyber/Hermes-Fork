"""Moteur et sessions SQLAlchemy.

PostgreSQL en exploitation ; SQLite en mémoire pour les tests unitaires hermétiques (les tests de
migration et d'intégration exigent PostgreSQL via ``OKXQ_TEST_DATABASE_URL``).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from okxq.persistence.models import Base


def make_engine(url: str, *, echo: bool = False) -> Engine:
    if url.startswith("sqlite"):
        engine = create_engine(
            url, echo=echo, future=True, connect_args={"check_same_thread": False}, poolclass=StaticPool
        )

        @event.listens_for(engine, "connect")
        def _fk_on(dbapi_connection, _record):  # type: ignore[no-untyped-def]
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

        return engine
    return create_engine(url, echo=echo, future=True, pool_pre_ping=True)


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


def create_all(engine: Engine) -> None:
    """Création directe du schéma (tests). En exploitation, utiliser les migrations Alembic."""
    Base.metadata.create_all(engine)


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def memory_engine() -> Engine:
    engine = make_engine("sqlite+pysqlite:///:memory:")
    create_all(engine)
    return engine
