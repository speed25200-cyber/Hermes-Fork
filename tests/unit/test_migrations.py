"""Tests hermétiques de la couche de migrations (§44, §67) : SQLite en mémoire, aucun réseau.

Ce que ces tests garantissent :

- la migration ``0001`` produit le même schéma que ``Base.metadata`` — tables, colonnes, nullabilité,
  clés primaires, unicité, contraintes de vérification, clés étrangères et index — donc personne ne
  peut ajouter un modèle sans migration correspondante ;
- ``downgrade`` ramène bien à une base vide (migration réversible, testée dans les deux sens) ;
- le DDL PostgreSQL *rendu* par la migration est identique à celui de ``create_all`` (TIMESTAMPTZ,
  JSONB, SERIAL, NUMERIC de précision explicite), ce qui se vérifie sans serveur ;
- la CLI ``okxq db`` refuse de détruire sans confirmation explicite et n'affiche jamais le mot de
  passe de l'URL de connexion.

Limite assumée : la comparaison par réflexion passe par un moteur SQLite. Les deux schémas comparés
(migration et ``create_all``) sont rendus par le même dialecte, donc l'égalité des types y est
significative *entre eux* ; en revanche SQLite ne distingue ni TIMESTAMPTZ de DATETIME, ni JSONB de
JSON, ni SERIAL d'un INTEGER. Le test de DDL PostgreSQL ci-dessous comble cet angle mort sur le
*texte* émis ; ce qu'il ne peut pas montrer — application réelle des contraintes, séquences,
``information_schema`` — est vérifié par ``tests/integration/test_postgres_schema.py``, qui reste
NOT_RUN sans ``OKXQ_TEST_DATABASE_URL``.
"""

from __future__ import annotations

import io
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import typer
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Connection, Engine, create_mock_engine, inspect, text
from sqlalchemy.exc import IntegrityError
from typer.testing import CliRunner

from okxq.cli_cmds import db_cmd
from okxq.persistence.db import make_engine, memory_engine
from okxq.persistence.models import ALL_TABLES, Base

MEMORY_URL = "sqlite+pysqlite:///:memory:"
# URL PostgreSQL sans mot de passe et sans hôte joignable : elle ne sert qu'à choisir le dialecte de
# rendu (mode hors ligne d'Alembic et moteur factice de SQLAlchemy). Aucune connexion n'est ouverte.
POSTGRES_URL = "postgresql+psycopg://utilisateur@serveur-inexistant/base"
EXPECTED_TABLE_COUNT = 30
VERSION_TABLE = "alembic_version"
ENV_VARS = ("OKXQ_DATABASE_URL", "DATABASE_URL", "OKXQ_SQLITE_PATH")


def _config(connection: Connection | None = None) -> Config:
    """Contexte Alembic visant le dossier de migrations réellement utilisé par la CLI."""
    cfg = Config()
    cfg.set_main_option("script_location", str(db_cmd.MIGRATIONS_DIR))
    if connection is not None:
        cfg.attributes["connection"] = connection
    return cfg


def _upgrade(engine: Engine, revision: str = "head") -> None:
    with engine.connect() as connection, connection.begin():
        command.upgrade(_config(connection), revision)


def _downgrade(engine: Engine, revision: str) -> None:
    with engine.connect() as connection, connection.begin():
        command.downgrade(_config(connection), revision)


def _snapshot(engine: Engine) -> dict[str, dict[str, Any]]:
    """Empreinte comparable du schéma réel, lue par réflexion (donc sans faire confiance au code).

    La table de version d'Alembic est exclue : elle n'existe que dans la base migrée, c'est son rôle.
    """
    inspector = inspect(engine)
    snapshot: dict[str, dict[str, Any]] = {}
    for table in sorted(inspector.get_table_names()):
        if table == VERSION_TABLE:
            continue
        snapshot[table] = {
            "columns": {
                column["name"]: (str(column["type"]), bool(column["nullable"]))
                for column in inspector.get_columns(table)
            },
            "primary_key": tuple(inspector.get_pk_constraint(table)["constrained_columns"]),
            "unique": sorted(
                (unique["name"], tuple(unique["column_names"]))
                for unique in inspector.get_unique_constraints(table)
            ),
            "checks": sorted(
                (check["name"], " ".join(str(check["sqltext"]).split()))
                for check in inspector.get_check_constraints(table)
            ),
            "foreign_keys": sorted(
                (
                    tuple(fk["constrained_columns"]),
                    fk["referred_table"],
                    tuple(fk["referred_columns"]),
                )
                for fk in inspector.get_foreign_keys(table)
            ),
            "indexes": sorted(
                (index["name"], tuple(index["column_names"]), bool(index["unique"]))
                for index in inspector.get_indexes(table)
            ),
        }
    return snapshot


@pytest.fixture
def migrated() -> Iterator[Engine]:
    """Base créée par la migration (le chemin d'exploitation)."""
    engine = make_engine(MEMORY_URL)
    _upgrade(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def declared() -> Iterator[Engine]:
    """Base créée par ``Base.metadata.create_all`` (la référence déclarative)."""
    engine = memory_engine()
    yield engine
    engine.dispose()


def test_migration_couvre_exactement_les_tables_declarees(migrated: Engine) -> None:
    tables = set(_snapshot(migrated))
    assert tables == set(ALL_TABLES)
    assert len(tables) == EXPECTED_TABLE_COUNT


def test_migration_produit_les_memes_colonnes_que_les_modeles(migrated: Engine, declared: Engine) -> None:
    after_migration = _snapshot(migrated)
    from_models = _snapshot(declared)
    assert set(after_migration) == set(from_models)
    for table in sorted(from_models):
        expected = from_models[table]["columns"]
        obtained = after_migration[table]["columns"]
        # Noms d'abord : un écart de nom est l'erreur la plus courante et la plus lisible.
        assert sorted(obtained) == sorted(expected), f"colonnes divergentes sur {table}"
        # Puis type rendu par SQLite et nullabilité, colonne par colonne.
        assert obtained == expected, f"types ou nullabilité divergents sur {table}"


def test_migration_produit_les_memes_contraintes_et_index(migrated: Engine, declared: Engine) -> None:
    after_migration = _snapshot(migrated)
    from_models = _snapshot(declared)
    for table in sorted(from_models):
        for aspect in ("primary_key", "unique", "checks", "foreign_keys", "indexes"):
            assert after_migration[table][aspect] == from_models[table][aspect], (
                f"{aspect} divergent sur {table}"
            )


def test_schema_migre_ne_diverge_pas_des_modeles(migrated: Engine) -> None:
    """Le comparateur d'Alembic — celui qu'utilise ``okxq db check`` — ne voit aucune divergence."""
    with migrated.connect() as connection:
        context = MigrationContext.configure(
            connection, opts={"compare_type": True, "compare_server_default": True}
        )
        assert compare_metadata(context, Base.metadata) == []


def test_revision_unique_et_enregistree_en_base(migrated: Engine) -> None:
    heads = tuple(ScriptDirectory.from_config(_config()).get_heads())
    assert heads == ("0001",), "une seule tête de révision : pas de branche de migration non résolue"
    with migrated.connect() as connection:
        assert MigrationContext.configure(connection).get_current_heads() == ("0001",)


def test_downgrade_ramene_a_une_base_vide() -> None:
    """Réversibilité exigée par le §44 : base vide -> 0001 -> base vide, sans résidu de table."""
    engine = make_engine(MEMORY_URL)
    try:
        _upgrade(engine)
        assert len(_snapshot(engine)) == EXPECTED_TABLE_COUNT
        _downgrade(engine, "base")
        assert _snapshot(engine) == {}
        assert inspect(engine).get_table_names() == [VERSION_TABLE]
        # Et la migration se rejoue sur la base ainsi vidée (reprise après incident).
        _upgrade(engine)
        assert len(_snapshot(engine)) == EXPECTED_TABLE_COUNT
    finally:
        engine.dispose()


_DECISION_SQL = (
    "INSERT INTO decisions (decision_id, mode, snapshot_id, cutoff_at, started_at, outcome, "
    "reason_codes, forecasts, edges, rejected_alternatives, timings_ms) "
    "VALUES ('d1', 'PAPER', 's1', '2026-01-01', '2026-01-01', 'NO_TRADE', '[]', '[]', '[]', '[]', '{}')"
)
_INTENT_SQL = (
    "INSERT INTO order_intents (intent_id, decision_id, account_scope, inst_id, side, contracts, "
    "order_type, reduce_only, ttl_ms, reason, client_order_id, payload_hash, created_at, expires_at) "
    "VALUES ('i1', 'd1', 'paper', 'TEST-USDT-SWAP', 'buy', {contracts}, 'limit', 0, 1000, 'test', "
    "'c1', 'h1', '2026-01-01', '2026-01-01')"
)


def test_migration_applique_les_contraintes_de_positivite(migrated: Engine) -> None:
    """Les CHECK du §44 (tailles positives) sont actifs en base, pas seulement déclarés en Python.

    L'insertion licite est exécutée aussi : sans elle, le test passerait même si la requête était
    rejetée pour une raison étrangère à la contrainte.
    """
    with migrated.begin() as connection:
        connection.execute(text(_DECISION_SQL))
        with pytest.raises(IntegrityError, match="ck_intent_contracts_positive"):
            connection.execute(text(_INTENT_SQL.format(contracts=0)))
    with migrated.begin() as connection:
        connection.execute(text(_INTENT_SQL.format(contracts=1)))


# --- rendu PostgreSQL, sans serveur --------------------------------------------------------------


def _clauses(statement: str) -> tuple[str, ...]:
    """Découpe un ordre SQL en tête + clauses triées.

    L'ordre des contraintes à l'intérieur d'un ``CREATE TABLE`` n'a aucune portée sémantique et
    diffère entre ``op.create_table`` et ``create_all``. Comparer des ensembles de clauses évite un
    faux échec tout en restant sensible au moindre changement de type, de longueur ou de nullabilité.
    """
    head, _, rest = statement.partition("(")
    if not rest:
        return (statement,)
    body = rest.rsplit(")", 1)[0]
    parts: list[str] = []
    current: list[str] = []
    depth = 0
    for character in body:
        if character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
        if character == "," and depth == 0:
            parts.append("".join(current).strip())
            current = []
        else:
            current.append(character)
    parts.append("".join(current).strip())
    return (head.strip(), *sorted(part for part in parts if part))


def _normalized(raw_statements: list[str]) -> list[tuple[str, ...]]:
    """Ordres SQL comparables : commentaires retirés, espaces normalisés, table de version exclue."""
    normalized: list[tuple[str, ...]] = []
    for raw in raw_statements:
        # Les lignes de commentaire sont retirées avant l'aplatissement, sinon un « -- » en tête
        # ferait disparaître l'ordre SQL qui le suit.
        code = " ".join(line for line in raw.splitlines() if not line.strip().startswith("--"))
        statement = " ".join(code.split()).rstrip(";").strip()
        if not statement or statement in ("BEGIN", "COMMIT") or VERSION_TABLE in statement:
            continue
        normalized.append(_clauses(statement))
    return sorted(normalized)


def _postgres_ddl_from_migration() -> list[tuple[str, ...]]:
    """DDL PostgreSQL que la migration produirait (mode ``--sql`` : aucune connexion ouverte)."""
    buffer = io.StringIO()
    cfg = Config(output_buffer=buffer, stdout=buffer)
    cfg.set_main_option("script_location", str(db_cmd.MIGRATIONS_DIR))
    cfg.attributes["okxq_url"] = POSTGRES_URL
    command.upgrade(cfg, "head", sql=True)
    return _normalized(buffer.getvalue().split(";"))


def _postgres_ddl_from_models() -> list[tuple[str, ...]]:
    """DDL PostgreSQL que ``create_all`` produirait, via un moteur factice (aucune connexion)."""
    collected: list[str] = []

    def executor(sql: Any, *args: Any, **kwargs: Any) -> None:
        collected.append(str(sql.compile(dialect=engine.dialect)))

    engine = create_mock_engine(POSTGRES_URL, executor)
    Base.metadata.create_all(engine)
    return _normalized(collected)


def test_ddl_postgresql_identique_entre_migration_et_modeles() -> None:
    """Comble l'angle mort de SQLite : le texte SQL émis sur PostgreSQL doit être le même des deux côtés."""
    assert _postgres_ddl_from_migration() == _postgres_ddl_from_models()


def test_ddl_postgresql_respecte_les_types_exiges() -> None:
    """§44 : TIMESTAMPTZ, JSONB et NUMERIC de précision explicite — vérifiés sur le DDL rendu."""
    flat = " ".join(" ".join(statement) for statement in _postgres_ddl_from_migration())
    assert "TIMESTAMP WITHOUT TIME ZONE" not in flat, "une date sans fuseau fausserait la causalité"
    assert "TIMESTAMP WITH TIME ZONE" in flat
    assert "JSONB" in flat
    # Un NUMERIC sans précision laisserait passer n'importe quelle valeur : il ne doit pas en rester.
    assert "NUMERIC " not in flat.replace("NUMERIC (", "NUMERIC(")
    assert "SERIAL" in flat, "les clés techniques doivent être servies par une séquence"


# --- CLI ``okxq db`` ------------------------------------------------------------------------------


def _isolated_env(monkeypatch: pytest.MonkeyPatch, url: str | None) -> None:
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    if url is not None:
        monkeypatch.setenv("OKXQ_DATABASE_URL", url)


def _sqlite_url(tmp_path: Path) -> str:
    return f"sqlite+pysqlite:///{tmp_path / 'okxq_test.db'}"


def test_cli_upgrade_puis_check_alignes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _isolated_env(monkeypatch, _sqlite_url(tmp_path))
    runner = CliRunner()
    upgraded = runner.invoke(db_cmd.app, ["upgrade"])
    assert upgraded.exit_code == 0, upgraded.output
    payload = json.loads(upgraded.output)
    assert payload["applied_now"] == ["0001"]
    assert payload["at_head"] is True
    # Aucune révision avant la première migration : absence de donnée, donc None (jamais 0 ni []).
    assert payload["revision_before"] is None

    checked = runner.invoke(db_cmd.app, ["check"])
    assert checked.exit_code == 0, checked.output
    assert json.loads(checked.output)["differences"] == []


def test_cli_check_sort_en_code_1_si_la_base_diverge(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    url = _sqlite_url(tmp_path)
    _isolated_env(monkeypatch, url)
    runner = CliRunner()
    assert runner.invoke(db_cmd.app, ["upgrade"]).exit_code == 0

    engine = make_engine(url)
    try:
        with engine.begin() as connection:
            connection.execute(text("DROP TABLE operator_actions"))
    finally:
        engine.dispose()

    result = runner.invoke(db_cmd.app, ["check"])
    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["aligned"] is False
    assert any(
        difference["kind"] == "add_table" and "operator_actions" in difference["details"]
        for difference in payload["differences"]
    ), payload["differences"]


def test_cli_downgrade_exige_le_mot_de_confirmation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Sans ``--confirmer EFFACER`` la base doit rester intacte : refus avant toute écriture."""
    url = _sqlite_url(tmp_path)
    _isolated_env(monkeypatch, url)
    runner = CliRunner()
    assert runner.invoke(db_cmd.app, ["upgrade"]).exit_code == 0

    for arguments in (
        ["downgrade", "--revision", "base"],
        ["downgrade", "--revision", "base", "--confirmer", "oui"],
    ):
        refused = runner.invoke(db_cmd.app, arguments)
        assert refused.exit_code == 1, arguments
        assert db_cmd.CONFIRM_WORD in refused.stderr

    engine = make_engine(url)
    try:
        assert len(_snapshot(engine)) == EXPECTED_TABLE_COUNT
    finally:
        engine.dispose()

    accepted = runner.invoke(db_cmd.app, ["downgrade", "--revision", "base", "--confirmer", "EFFACER"])
    assert accepted.exit_code == 0, accepted.output
    payload = json.loads(accepted.output)
    assert payload["destructive"] is True
    assert payload["removed_now"] == ["0001"]


def test_cli_downgrade_sans_revision_est_refuse(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Aucune révision cible par défaut : une commande destructrice ne s'improvise pas (§44)."""
    _isolated_env(monkeypatch, _sqlite_url(tmp_path))
    result = CliRunner().invoke(db_cmd.app, ["downgrade", "--confirmer", "EFFACER"])
    assert result.exit_code != 0


def test_cli_refuse_de_migrer_sans_base_designee(monkeypatch: pytest.MonkeyPatch) -> None:
    _isolated_env(monkeypatch, None)
    result = CliRunner().invoke(db_cmd.app, ["upgrade"])
    assert result.exit_code == 1
    assert "OKXQ_DATABASE_URL" in result.stderr


def test_cli_history_sans_base_ne_pretend_rien(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sans base consultée, l'état d'application d'une révision est inconnu : ``None``, pas ``False``."""
    _isolated_env(monkeypatch, None)
    result = CliRunner().invoke(db_cmd.app, ["history"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["database"] is None
    assert [revision["applied"] for revision in payload["revisions"]] == [None]


def test_url_masquee_ne_revele_pas_le_mot_de_passe() -> None:
    masked = db_cmd._masked("postgresql+psycopg://okxq:tr3s-secret@db.interne:5432/okxq")
    assert "tr3s-secret" not in masked
    assert "***" in masked
    assert "db.interne" in masked  # l'hôte n'est pas un secret et sert au diagnostic


def test_url_illisible_refusee_sans_afficher_son_contenu(monkeypatch: pytest.MonkeyPatch) -> None:
    _isolated_env(monkeypatch, "ceci-nest-pas-une-url-secret123")
    with pytest.raises(typer.Exit) as raised:
        db_cmd._required_url()
    assert raised.value.exit_code == 1
