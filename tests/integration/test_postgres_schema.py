"""Vérifie sur PostgreSQL ce que SQLite ne peut pas montrer (§44) : TIMESTAMPTZ, JSONB, NUMERIC réels.

Ce module est marqué ``integration`` et **saute** sans ``OKXQ_TEST_DATABASE_URL`` : un test non
exécuté reste NOT_RUN, il ne devient jamais PASS. Rien ici ne fabrique une base : c'est l'opérateur
qui en désigne une (``make db-up`` puis ``export OKXQ_TEST_DATABASE_URL=...``).

Pourquoi un schéma jetable plutôt que ``public`` : la migration ``downgrade`` est destructive. Chaque
exécution crée ``okxq_mig_<aléa>``, y épingle le ``search_path``, migre, vérifie, puis détruit ce seul
schéma. Une base d'exploitation pointée par erreur ne peut donc pas perdre ses tables.

Contrainte d'exécution : la fixture hermétique ``no_network`` (autouse, ``tests/conftest.py``) bloque
les sockets sortantes sauf vers ``localhost``/``127.0.0.1``. Ces tests visent donc un PostgreSQL local
(conteneur de ``make db-up``) ou une socket Unix ; une base distante exigerait le marqueur
``connected``, qui n'a pas lieu d'être ici.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from sqlalchemy import Connection, Engine, create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError, IntegrityError

from okxq.cli_cmds.db_cmd import MIGRATIONS_DIR
from okxq.persistence.models import ALL_TABLES, Base

URL_ENV_VAR = "OKXQ_TEST_DATABASE_URL"
EXPECTED_TABLE_COUNT = 30
VERSION_TABLE = "alembic_version"

_RAW_URL = os.environ.get(URL_ENV_VAR)


def _backend_name(raw: str | None) -> str | None:
    """Moteur visé, ou ``None`` si indéterminable.

    Une URL illisible ne doit pas casser la *collecte* des tests hermétiques ; elle fera échouer
    bruyamment ces tests-ci, ce qui est le bon comportement : l'opérateur a désigné une base.
    """
    if not raw:
        return None
    try:
        return make_url(raw).get_backend_name()
    except ArgumentError:
        return None


_BACKEND = _backend_name(_RAW_URL)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not _RAW_URL,
        reason=f"{URL_ENV_VAR} absent : NOT_RUN (aucune base PostgreSQL désignée)",
    ),
    pytest.mark.skipif(
        _BACKEND is not None and _BACKEND != "postgresql",
        reason=f"{URL_ENV_VAR} ne désigne pas PostgreSQL : NOT_RUN (ces vérifications n'ont de sens que là)",
    ),
]

T0 = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)


def _url() -> str:
    """URL normalisée sur le pilote réellement installé (psycopg 3) ; jamais journalisée."""
    assert _RAW_URL is not None
    parsed = make_url(_RAW_URL)
    if parsed.drivername == "postgresql":
        parsed = parsed.set(drivername="postgresql+psycopg")
    return parsed.render_as_string(hide_password=False)


def _alembic_config(connection: Connection) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.attributes["connection"] = connection
    return cfg


@pytest.fixture
def scratch_engine() -> Iterator[tuple[Engine, str]]:
    """Moteur épinglé sur un schéma jetable, détruit quoi qu'il arrive à la fin du test."""
    schema = f"okxq_mig_{uuid4().hex[:12]}"
    admin = create_engine(_url(), future=True, pool_pre_ping=True)
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(
        _url(), future=True, pool_pre_ping=True, connect_args={"options": f"-csearch_path={schema}"}
    )
    try:
        yield engine, schema
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        admin.dispose()


def _upgrade(engine: Engine, revision: str = "head") -> None:
    with engine.connect() as connection, connection.begin():
        command.upgrade(_alembic_config(connection), revision)


def _downgrade(engine: Engine, revision: str) -> None:
    with engine.connect() as connection, connection.begin():
        command.downgrade(_alembic_config(connection), revision)


def _tables(engine: Engine, schema: str) -> set[str]:
    return {name for name in inspect(engine).get_table_names(schema=schema) if name != VERSION_TABLE}


def test_migration_cree_le_schema_complet(scratch_engine: tuple[Engine, str]) -> None:
    engine, schema = scratch_engine
    _upgrade(engine)
    tables = _tables(engine, schema)
    assert tables == set(ALL_TABLES)
    assert len(tables) == EXPECTED_TABLE_COUNT


def test_schema_migre_ne_diverge_pas_des_modeles(scratch_engine: tuple[Engine, str]) -> None:
    """Le comparateur d'Alembic (celui d'``okxq db check``) ne voit rien à corriger sur PostgreSQL."""
    engine, _schema = scratch_engine
    _upgrade(engine)
    with engine.connect() as connection:
        context = MigrationContext.configure(
            connection, opts={"compare_type": True, "compare_server_default": True}
        )
        assert compare_metadata(context, Base.metadata) == []


def test_aucune_date_sans_fuseau_ni_json_non_binaire(scratch_engine: tuple[Engine, str]) -> None:
    """§44 : TIMESTAMPTZ partout et JSONB partout. Une seule colonne naïve suffit à fausser la causalité."""
    engine, schema = scratch_engine
    _upgrade(engine)
    with engine.connect() as connection:
        naives = connection.execute(
            text(
                "SELECT table_name, column_name FROM information_schema.columns "
                "WHERE table_schema = :schema AND data_type = 'timestamp without time zone' "
                "ORDER BY table_name, column_name"
            ),
            {"schema": schema},
        ).all()
        plain_json = connection.execute(
            text(
                "SELECT table_name, column_name FROM information_schema.columns "
                "WHERE table_schema = :schema AND data_type = 'json' "
                "ORDER BY table_name, column_name"
            ),
            {"schema": schema},
        ).all()
        timestamptz = connection.execute(
            text(
                "SELECT count(*) FROM information_schema.columns "
                "WHERE table_schema = :schema AND data_type = 'timestamp with time zone'"
            ),
            {"schema": schema},
        ).scalar_one()
    assert naives == [], f"colonnes horodatées sans fuseau : {naives}"
    assert plain_json == [], f"colonnes JSON non binaires : {plain_json}"
    assert timestamptz > 0, "aucune colonne TIMESTAMPTZ : le schéma n'a pas été migré"


def test_precisions_numeriques_documentees(scratch_engine: tuple[Engine, str]) -> None:
    """Les NUMERIC portent la précision documentée (§44) : sans elle, les tests de dépassement mentent."""
    engine, schema = scratch_engine
    _upgrade(engine)
    expected = {
        ("fills", "fee_cashflow"): (28, 8),
        ("fills", "fill_price"): (28, 12),
        ("fills", "contracts"): (28, 12),
        ("ledger_entries", "amount"): (28, 8),
        ("account_snapshots", "equity"): (28, 8),
        ("account_snapshots", "unit_value"): (18, 12),
        ("instrument_versions", "base_units_per_contract"): (28, 12),
    }
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT table_name, column_name, numeric_precision, numeric_scale "
                "FROM information_schema.columns "
                "WHERE table_schema = :schema AND data_type = 'numeric'"
            ),
            {"schema": schema},
        ).all()
    observed = {(row[0], row[1]): (row[2], row[3]) for row in rows}
    for key, precision in expected.items():
        assert observed.get(key) == precision, f"précision inattendue pour {key}"
    # Aucune colonne NUMERIC sans précision explicite : PostgreSQL accepterait alors n'importe quoi.
    assert [key for key, value in observed.items() if value[0] is None] == []


def test_cles_primaires_entieres_sont_sequentielles(scratch_engine: tuple[Engine, str]) -> None:
    """Les identifiants techniques doivent être servis par une séquence, pas fournis par l'appelant."""
    engine, schema = scratch_engine
    _upgrade(engine)
    with engine.connect() as connection:
        default = connection.execute(
            text(
                "SELECT column_default FROM information_schema.columns "
                "WHERE table_schema = :schema AND table_name = 'ledger_entries' AND column_name = 'id'"
            ),
            {"schema": schema},
        ).scalar_one()
    assert default is not None and "nextval" in default


def test_unicite_durable_du_client_order_id(scratch_engine: tuple[Engine, str]) -> None:
    """§44 : (account_scope, client_order_id) est unique en base, pas seulement dans le code applicatif."""
    engine, _schema = scratch_engine
    _upgrade(engine)
    decisions = Base.metadata.tables["decisions"]
    intents = Base.metadata.tables["order_intents"]
    intent_values = {
        "decision_id": "d-1",
        "account_scope": "paper",
        "inst_id": "TEST-USDT-SWAP",
        "side": "buy",
        "contracts": Decimal("1"),
        "order_type": "limit",
        "reduce_only": False,
        "ttl_ms": 1000,
        "reason": "test",
        "client_order_id": "cli-1",
        "payload_hash": "h" * 8,
        "created_at": T0,
        "expires_at": T0,
    }
    with engine.begin() as connection:
        connection.execute(
            decisions.insert().values(
                decision_id="d-1",
                mode="PAPER",
                snapshot_id="s-1",
                cutoff_at=T0,
                started_at=T0,
                outcome="TRADE",
            )
        )
        connection.execute(intents.insert().values(intent_id="i-1", **intent_values))
    # Transaction distincte : le refus doit venir de la base, pas d'un cache de session.
    with engine.begin() as connection:
        with pytest.raises(IntegrityError, match="uq_intent_client_order_id"):
            connection.execute(intents.insert().values(intent_id="i-2", **intent_values))


def test_contraintes_de_positivite_actives(scratch_engine: tuple[Engine, str]) -> None:
    """Les CHECK de tailles positives (§44) sont appliqués par PostgreSQL, pas seulement déclarés."""
    engine, _schema = scratch_engine
    _upgrade(engine)
    intents = Base.metadata.tables["order_intents"]
    decisions = Base.metadata.tables["decisions"]
    with engine.begin() as connection:
        connection.execute(
            decisions.insert().values(
                decision_id="d-2",
                mode="PAPER",
                snapshot_id="s-2",
                cutoff_at=T0,
                started_at=T0,
                outcome="TRADE",
            )
        )
    # Transaction distincte, même raison : la contrainte est vérifiée à l'écriture réelle.
    with engine.begin() as connection:
        with pytest.raises(IntegrityError, match="ck_intent_contracts_positive"):
            connection.execute(
                intents.insert().values(
                    intent_id="i-3",
                    decision_id="d-2",
                    account_scope="paper",
                    inst_id="TEST-USDT-SWAP",
                    side="buy",
                    contracts=Decimal("0"),
                    order_type="limit",
                    reduce_only=False,
                    ttl_ms=1000,
                    reason="test",
                    client_order_id="cli-3",
                    payload_hash="h" * 8,
                    created_at=T0,
                    expires_at=T0,
                )
            )


def test_migration_reversible_puis_rejouable(scratch_engine: tuple[Engine, str]) -> None:
    """§44 : la migration est testée sur base vide ET depuis la version précédente."""
    engine, schema = scratch_engine
    _upgrade(engine)
    assert len(_tables(engine, schema)) == EXPECTED_TABLE_COUNT
    _downgrade(engine, "base")
    assert _tables(engine, schema) == set()
    _upgrade(engine)
    assert len(_tables(engine, schema)) == EXPECTED_TABLE_COUNT
