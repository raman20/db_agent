"""Introspection, credential-hygiene and lifecycle tests for schemapilot.db.

No test here needs a server: SQLite and DuckDB run in-process, and the Trino case is pure URL
construction.
"""

import json
import logging
import os
import stat

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from schemapilot import db as db_module
from schemapilot.db import DatabaseManager


@pytest.fixture
def duckdb_db(tmp_path, isolated_config):
    """A file-backed DuckDB with two user schemas (`main` and `other`)."""
    pytest.importorskip("duckdb_engine")

    path = tmp_path / "dtest.duckdb"
    engine = create_engine(f"duckdb:///{path}")
    with engine.connect() as conn:
        conn.execute(text("CREATE SCHEMA other"))
        conn.execute(text("CREATE TABLE main.parent (pid INTEGER PRIMARY KEY, nm TEXT)"))
        # DuckDB refuses cross-schema foreign keys, so the FK lives inside `main`.
        conn.execute(text("CREATE TABLE main.child (cid INTEGER, pid INTEGER REFERENCES main.parent(pid))"))
        conn.execute(text("CREATE TABLE other.widget (wid INTEGER, label TEXT)"))
        conn.commit()
    engine.dispose()

    manager = DatabaseManager()
    manager.add_connection("duck", {"name": "duck", "db_type": "duckdb", "path": str(path)})
    manager.select_connection("duck")
    yield manager
    manager._dispose_engine()


# --------------------------------------------------------------------------- introspection


def test_sqlite_primary_key_and_foreign_key(sqlite_db):
    """Direct regression for the removed inspector.get_primary_keys(): PKs must be marked."""
    meta = sqlite_db.get_schema_metadata()

    assert set(meta["tables"]) == {"parent", "child"}
    parent_cols = {c["name"]: c for c in meta["tables"]["parent"]["columns"]}
    assert parent_cols["pid"]["is_primary"] is True
    assert parent_cols["nm"]["is_primary"] is False
    assert parent_cols["nm"]["nullable"] is False

    assert meta["relationships"] == [
        {"from_table": "child", "from_columns": ["pid"], "to_table": "parent", "to_columns": ["pid"]}
    ]
    assert meta["truncated"] is False
    assert meta["total_tables"] == 2


def test_metadata_reports_totals_and_truncation(sqlite_db):
    meta = sqlite_db.get_schema_metadata(max_tables=1)

    assert meta["truncated"] is True
    # total_tables reports what EXISTS, not what fitted inside the cap.
    assert meta["total_tables"] == 2
    assert len(meta["tables"]) == 1


def test_default_max_tables_comes_from_settings(sqlite_db, monkeypatch):
    from schemapilot.config import settings

    monkeypatch.setattr(settings, "CATALOG_MAX_TABLES", 1)
    assert sqlite_db.get_schema_metadata()["truncated"] is True


def test_per_table_error_is_skipped_not_fatal(sqlite_db, monkeypatch):
    """A single unreadable table must not cost the user the whole catalog."""
    from sqlalchemy.engine.reflection import Inspector

    original = Inspector.get_columns

    def flaky(self, table_name, schema=None, **kw):
        if table_name == "child":
            raise RuntimeError("boom")
        return original(self, table_name, schema=schema, **kw)

    monkeypatch.setattr(Inspector, "get_columns", flaky)

    meta = sqlite_db.get_schema_metadata()
    assert set(meta["tables"]) == {"parent"}
    assert meta["total_tables"] == 2
    # Partial attrition is reported but is not a wholesale failure.
    assert meta["skipped_tables"] == ["child"]
    assert meta["reflection_failed"] is False


def test_total_reflection_failure_is_loud_not_an_empty_catalog(sqlite_db, monkeypatch, caplog):
    """Losing EVERY table is a defect, not tolerable attrition, and must be visible.

    This is the failure mode that hid the ClickHouse inspect(engine) bug: each table was caught
    by the tolerant per-table handler, so the caller saw a clean-looking empty catalog with a
    correct total_tables and no complaint.
    """
    from sqlalchemy.engine.reflection import Inspector

    monkeypatch.setattr(
        Inspector, "get_columns",
        lambda self, table_name, schema=None, **kw: (_ for _ in ()).throw(RuntimeError("no execute")),
    )

    with caplog.at_level(logging.ERROR, logger="schemapilot.db"):
        meta = sqlite_db.get_schema_metadata()

    assert meta["tables"] == {}
    assert meta["total_tables"] == 2
    assert meta["reflection_failed"] is True
    assert sorted(meta["skipped_tables"]) == ["child", "parent"]
    assert any(record.levelno >= logging.ERROR for record in caplog.records)


def test_empty_database_is_not_reported_as_a_reflection_failure(tmp_path, isolated_config):
    """An genuinely empty schema must stay distinguishable from a failed reflection."""
    path = tmp_path / "empty.db"
    engine = create_engine(f"sqlite:///{path}")
    engine.connect().close()
    engine.dispose()

    manager = DatabaseManager()
    manager.add_connection("empty", {"name": "empty", "db_type": "sqlite", "path": str(path)})
    manager.select_connection("empty")

    meta = manager.get_schema_metadata()
    assert meta["tables"] == {}
    assert meta["total_tables"] == 0
    assert meta["reflection_failed"] is False
    manager._dispose_engine()


def test_inspector_is_bound_to_a_connection_not_the_engine(sqlite_db):
    """Regression: clickhouse-connect's inspector calls the 1.x `bind.execute()`.

    A Connection has it, an Engine does not in SQLAlchemy 2.x, so binding to the Engine made
    every ClickHouse table fail reflection and returned an empty catalog.
    """
    from sqlalchemy.engine import Connection

    seen = []
    original_inspect = db_module.inspect

    def recording_inspect(target):
        seen.append(target)
        return original_inspect(target)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(db_module, "inspect", recording_inspect)
        sqlite_db.get_schema_metadata()

    assert seen and all(isinstance(target, Connection) for target in seen)


def test_duckdb_qualified_keys_and_composite_schema_parsing(duckdb_db):
    """DuckDB's get_schema_names() returns `db.schema` composites; system entries are dropped."""
    meta = duckdb_db.get_schema_metadata()

    assert set(meta["tables"]) == {"dtest.main.parent", "dtest.main.child", "dtest.other.widget"}
    # No system.*/temp.* schema leaked into the catalog.
    assert not [name for name in meta["tables"] if name.startswith(("system.", "temp."))]

    assert {
        "from_table": "dtest.main.child",
        "from_columns": ["pid"],
        "to_table": "dtest.main.parent",
        "to_columns": ["pid"],
    } in meta["relationships"]


def test_duckdb_primary_keys_are_a_known_limitation(duckdb_db):
    """KNOWN LIMITATION: DuckDB reflection reports no PK even for INTEGER PRIMARY KEY.

    Asserted deliberately so the day duckdb-engine starts reporting PKs, this test fails and we
    can drop the caveat from DUCKDB.prompt_notes.
    """
    meta = duckdb_db.get_schema_metadata()
    parent = meta["tables"]["dtest.main.parent"]
    assert all(col["is_primary"] is False for col in parent["columns"])

    from schemapilot.engines import get_spec

    assert "primary key" in get_spec("duckdb").prompt_notes.lower()


# --------------------------------------------------------------------------- schema precedence


def test_schema_precedence_explicit_beats_profile_beats_enumeration(duckdb_db):
    """explicit `schemas=` > profile `schema` > enumeration (see db._resolve_schemas)."""
    profile = duckdb_db.connections["duck"]

    # (3) no schema anywhere -> enumerate both user schemas.
    assert set(duckdb_db.get_schema_metadata()["tables"]) == {
        "dtest.main.parent",
        "dtest.main.child",
        "dtest.other.widget",
    }

    # (2) profile schema wins over enumeration.
    profile["schema"] = "dtest.other"
    assert set(duckdb_db.get_schema_metadata()["tables"]) == {"dtest.other.widget"}

    # (1) explicit argument wins over the profile schema.
    assert set(duckdb_db.get_schema_metadata(schemas=["dtest.main"])["tables"]) == {
        "dtest.main.parent",
        "dtest.main.child",
    }


def test_single_part_engine_ignores_schema_enumeration(sqlite_db):
    """SQLite has 1-part names, so no schema is ever passed to the inspector."""
    inspector_schemas = sqlite_db._resolve_schemas(
        None, sqlite_db.active_spec, sqlite_db.active_config, None
    )
    assert inspector_schemas == [None]


@pytest.mark.parametrize(
    "db_type, namespace_field",
    [("postgres", "schema"), ("trino", "schema"), ("mysql", "database"), ("clickhouse", "database")],
)
def test_specs_declare_which_profile_key_holds_the_namespace(db_type, namespace_field):
    """The namespace key is spec data because it differs from the SQL concept.

    MySQL and ClickHouse call their schema level a "database"; assuming `schema` everywhere made
    step 2 of the precedence silently unreachable for both.
    """
    from schemapilot.engines import get_spec

    assert get_spec(db_type).introspection_namespace_field == namespace_field


def test_sqlite_declares_no_namespace_field():
    """SQLite has 1-part names, so there is no namespace to scope to."""
    from schemapilot.engines import get_spec

    assert get_spec("sqlite").introspection_namespace_field is None


def test_duckdb_uses_its_optional_schema_key():
    """DuckDB connects by path but may still carry a `db.schema` composite to scope to."""
    from schemapilot.engines import get_spec

    assert get_spec("duckdb").introspection_namespace_field == "schema"


@pytest.mark.parametrize("db_type", ["mysql", "clickhouse"])
def test_configured_database_wins_over_enumeration(isolated_config, monkeypatch, db_type):
    """For MySQL/ClickHouse the profile's `database` is the namespace and must beat enumeration.

    Regression: `_resolve_schemas` looked only at `config["schema"]`, which these engines never
    collect, so both enumerated every accessible database. Verified live on ClickHouse, whose
    enumeration returns `default` alongside the configured database.
    """
    manager = DatabaseManager()
    manager.add_connection("target", {
        "name": "target",
        "db_type": db_type,
        "host": "127.0.0.1",
        "username": "u",
        "password": "p",
        "database": "schemapilot_db",
    })
    manager.active_id = "target"

    spec = manager.active_spec

    def fail_if_called(*args, **kwargs):
        raise AssertionError("enumeration must not run when the profile names a namespace")

    monkeypatch.setattr(manager, "_enumerate_schemas", fail_if_called)

    assert manager._resolve_schemas(None, spec, manager.active_config, None) == ["schemapilot_db"]
    # An explicit argument still outranks the profile.
    assert manager._resolve_schemas(None, spec, manager.active_config, ["other"]) == ["other"]


def test_postgres_schema_key_still_wins_over_its_database_key(isolated_config, monkeypatch):
    """Postgres collects both keys; only `schema` is the namespace."""
    manager = DatabaseManager()
    manager.add_connection("pg", {
        "name": "pg",
        "db_type": "postgres",
        "host": "127.0.0.1",
        "database": "analytics",
        "schema": "reporting",
    })
    manager.active_id = "pg"

    monkeypatch.setattr(
        manager, "_enumerate_schemas",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not enumerate")),
    )
    assert manager._resolve_schemas(None, manager.active_spec, manager.active_config, None) == ["reporting"]


# --------------------------------------------------------------------------- URIs and specs


def test_trino_uri_builds_without_a_server(isolated_config):
    manager = DatabaseManager()
    uri = manager.build_uri({
        "db_type": "trino",
        "host": "trino.internal",
        "port": 8080,
        "username": "analyst",
        "catalog": "tpch",
        "schema": "tiny",
    })

    parsed = make_url(str(uri))
    assert parsed.drivername == "trino"
    assert parsed.host == "trino.internal"
    assert parsed.database == "tpch/tiny"


def test_build_uri_preserves_awkward_passwords(isolated_config):
    """The f-string version parsed this password as `p` and the host as `ss`."""
    manager = DatabaseManager()
    uri = manager.build_uri({
        "db_type": "postgres",
        "host": "10.0.3.9",
        "username": "svc",
        "password": "p@ss/w:rd",
        "database": "analytics",
    })

    assert uri.password == "p@ss/w:rd"
    assert uri.host == "10.0.3.9"


def test_unknown_engine_names_the_supported_set(isolated_config):
    manager = DatabaseManager()
    with pytest.raises(ValueError) as exc:
        manager.build_uri({"db_type": "oracle"})
    assert "trino" in str(exc.value) and "sqlite" in str(exc.value)


def test_active_spec_reflects_the_active_connection(sqlite_db):
    assert sqlite_db.active_spec.name == "sqlite"
    assert sqlite_db.active_spec.sqlglot_dialect == "sqlite"


def test_no_explain_query_method(sqlite_db):
    """EXPLAIN preflight is deferred; an unused method would be dead code."""
    assert not hasattr(sqlite_db, "explain_query")


def test_missing_driver_reports_the_pip_command(isolated_config, monkeypatch):
    from sqlalchemy.exc import NoSuchModuleError

    manager = DatabaseManager()

    def boom(*args, **kwargs):
        raise NoSuchModuleError("Can't load plugin: sqlalchemy.dialects:trino")

    monkeypatch.setattr(db_module, "create_engine", boom)

    ok, message = manager.test_connection({"db_type": "trino", "host": "h", "catalog": "tpch"})
    assert ok is False
    assert "pip install 'schemapilot[trino]'" in message


# --------------------------------------------------------------------------- lifecycle


def test_engine_is_disposed_on_switch(tmp_path, isolated_config):
    first = tmp_path / "a.db"
    second = tmp_path / "b.db"
    for path in (first, second):
        engine = create_engine(f"sqlite:///{path}")
        with engine.connect() as conn:
            conn.execute(text("CREATE TABLE t (id INTEGER PRIMARY KEY)"))
            conn.commit()
        engine.dispose()

    manager = DatabaseManager()
    manager.add_connection("a", {"name": "a", "db_type": "sqlite", "path": str(first)})
    manager.add_connection("b", {"name": "b", "db_type": "sqlite", "path": str(second)})

    manager.select_connection("a")
    old_engine = manager.engine
    disposed = []
    old_engine.dispose = lambda *a, **k: disposed.append(True)

    manager.select_connection("b")

    assert disposed, "the previous engine must be disposed when switching connections"
    assert manager.engine is not old_engine
    manager._dispose_engine()


def test_failed_switch_keeps_the_working_connection(tmp_path, isolated_config, monkeypatch):
    """A failed switch must be a no-op, not a downgrade to a half-broken state.

    The old ordering disposed the current engine before proving the candidate, so on failure
    `active_id`/`active_spec` still named the old profile while `engine` was None -- the REPL
    advertised an active connection that could not execute anything.
    """
    good = tmp_path / "good.db"
    engine = create_engine(f"sqlite:///{good}")
    with engine.connect() as conn:
        conn.execute(text("CREATE TABLE t (id INTEGER PRIMARY KEY)"))
        conn.commit()
    engine.dispose()

    manager = DatabaseManager()
    manager.add_connection("good", {"name": "good", "db_type": "sqlite", "path": str(good)})
    manager.add_connection("broken", {
        "name": "broken", "db_type": "postgres", "host": "127.0.0.1", "port": 1, "database": "nope",
    })
    manager.select_connection("good")

    working_engine = manager.engine
    working_uri = str(working_engine.url)

    monkeypatch.setattr(
        manager, "_probe",
        lambda engine, spec: (_ for _ in ()).throw(RuntimeError("connection refused")),
    )

    with pytest.raises(ConnectionError):
        manager.select_connection("broken")

    # Every piece of state still describes `good`, and they agree with each other: active_spec
    # and active_config are derived from active_id, so a mismatch here means the swap tore.
    assert manager.active_id == "good"
    assert manager.active_spec.name == "sqlite"
    assert manager.active_config["name"] == "good"
    assert manager.engine is working_engine
    # The engine still points at the profile active_id names -- not at the rejected candidate.
    assert str(manager.engine.url) == working_uri
    assert str(manager.engine.url).endswith("good.db")
    # And it is genuinely still usable, not merely non-None.
    assert manager.execute_query("SELECT 1 AS one")["rows"] == [{"one": 1}]
    assert "t" in manager.get_schema_metadata()["tables"]
    manager._dispose_engine()


def test_failed_switch_disposes_only_the_candidate(tmp_path, isolated_config, monkeypatch):
    """The rejected candidate's pool must not leak, and the live engine must survive."""
    good = tmp_path / "good.db"
    engine = create_engine(f"sqlite:///{good}")
    engine.connect().close()
    engine.dispose()

    manager = DatabaseManager()
    manager.add_connection("good", {"name": "good", "db_type": "sqlite", "path": str(good)})
    manager.add_connection("bad", {"name": "bad", "db_type": "sqlite", "path": str(tmp_path / "bad.db")})
    manager.select_connection("good")

    live_engine = manager.engine
    live_disposed = []
    live_engine.dispose = lambda *a, **k: live_disposed.append(True)

    candidates = []
    real_create_engine = db_module.create_engine

    def tracking(*args, **kwargs):
        created = real_create_engine(*args, **kwargs)
        candidates.append(created)
        disposed = []
        created.dispose = lambda *a, **k: disposed.append(True)
        created._disposed_calls = disposed
        return created

    monkeypatch.setattr(db_module, "create_engine", tracking)
    monkeypatch.setattr(
        manager, "_probe", lambda engine, spec: (_ for _ in ()).throw(RuntimeError("nope"))
    )

    with pytest.raises(ConnectionError):
        manager.select_connection("bad")

    assert len(candidates) == 1
    assert candidates[0]._disposed_calls, "the rejected candidate engine must be disposed"
    assert not live_disposed, "the working engine must NOT be disposed by a failed switch"


def test_failed_switch_does_not_repersist_the_active_flag(tmp_path, isolated_config, monkeypatch):
    """A failed switch must not rewrite connections.json to point at the broken profile."""
    good = tmp_path / "good.db"
    engine = create_engine(f"sqlite:///{good}")
    engine.connect().close()
    engine.dispose()

    manager = DatabaseManager()
    manager.add_connection("good", {"name": "good", "db_type": "sqlite", "path": str(good)})
    manager.add_connection("bad", {"name": "bad", "db_type": "sqlite", "path": str(tmp_path / "bad.db")})
    manager.select_connection("good")

    monkeypatch.setattr(
        manager, "_probe", lambda engine, spec: (_ for _ in ()).throw(RuntimeError("nope"))
    )
    with pytest.raises(ConnectionError):
        manager.select_connection("bad")

    with open(db_module.CONNECTIONS_FILE) as f:
        persisted = json.load(f)
    assert persisted["good"]["is_active"] is True
    assert persisted["bad"].get("is_active") is not True
    manager._dispose_engine()


def test_test_connection_disposes_its_throwaway_engine(isolated_config, monkeypatch, tmp_path):
    """`test_connection` must release the pool it opened, even though nothing else holds it.

    Pool counters cannot prove this: a fresh, never-disposed pool also reports
    ``checkedin() == 0``. So spy on the engine's own ``dispose`` and assert it was called,
    then confirm the real dispose ran by checking SQLAlchemy swapped in a fresh pool object.
    """
    manager = DatabaseManager()
    created = []
    dispose_calls = []
    real_create_engine = db_module.create_engine

    pools_at_creation = []

    def tracking_create_engine(*args, **kwargs):
        engine = real_create_engine(*args, **kwargs)
        pools_at_creation.append(engine.pool)
        real_dispose = engine.dispose

        def spy_dispose(*a, **kw):
            dispose_calls.append(engine)
            return real_dispose(*a, **kw)

        engine.dispose = spy_dispose
        created.append(engine)
        return engine

    monkeypatch.setattr(db_module, "create_engine", tracking_create_engine)

    ok, _ = manager.test_connection({"db_type": "sqlite", "path": str(tmp_path / "probe.db")})
    assert ok
    assert len(created) == 1
    throwaway = created[0]

    assert dispose_calls == [throwaway], "the throwaway engine must be disposed exactly once"
    # The real dispose() ran, not just the spy: SQLAlchemy recreates the pool object.
    assert throwaway.pool is not pools_at_creation[0]


def test_startup_selects_the_persisted_active_connection(tmp_path, isolated_config):
    """auto_connect_active honours `is_active`, not dict insertion order."""
    first = tmp_path / "first.db"
    chosen = tmp_path / "chosen.db"
    for path in (first, chosen):
        engine = create_engine(f"sqlite:///{path}")
        with engine.connect() as conn:
            conn.execute(text("CREATE TABLE t (id INTEGER PRIMARY KEY)"))
            conn.commit()
        engine.dispose()

    db_module.write_credential_json(db_module.CONNECTIONS_FILE, {
        "first": {"name": "first", "db_type": "sqlite", "path": str(first), "is_active": False},
        "chosen": {"name": "chosen", "db_type": "sqlite", "path": str(chosen), "is_active": True},
    })

    manager = DatabaseManager()
    assert manager.active_id == "chosen"
    manager._dispose_engine()


def test_startup_falls_back_to_first_profile_when_none_flagged(tmp_path, isolated_config):
    path = tmp_path / "only.db"
    engine = create_engine(f"sqlite:///{path}")
    with engine.connect() as conn:
        conn.execute(text("CREATE TABLE t (id INTEGER PRIMARY KEY)"))
        conn.commit()
    engine.dispose()

    db_module.write_credential_json(db_module.CONNECTIONS_FILE, {
        "only": {"name": "only", "db_type": "sqlite", "path": str(path)},
    })

    manager = DatabaseManager()
    assert manager.active_id == "only"
    manager._dispose_engine()


# --------------------------------------------------------------------------- credential hygiene


def test_config_dir_is_0700_and_connections_file_is_0600(isolated_config):
    manager = DatabaseManager()
    manager.add_connection("c1", {"name": "c1", "db_type": "sqlite", "path": "x.db"})

    assert stat.S_IMODE(os.stat(isolated_config).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(db_module.CONNECTIONS_FILE).st_mode) == 0o600


def test_models_file_is_0600(isolated_config):
    from schemapilot.llm import ModelProfileManager, MODELS_FILE

    manager = ModelProfileManager()
    manager.add_profile("default", {"provider": "google", "model_name": "gemini-2.0-flash", "api_key": "secret"})

    assert stat.S_IMODE(os.stat(MODELS_FILE).st_mode) == 0o600
    with open(MODELS_FILE) as f:
        assert json.load(f)["default"]["model_name"] == "gemini-2.0-flash"


def test_existing_profiles_without_optional_keys_still_work(tmp_path, isolated_config):
    """No migration step: `catalog`/`schema`/`include_samples`/... are optional."""
    path = tmp_path / "legacy.db"
    engine = create_engine(f"sqlite:///{path}")
    with engine.connect() as conn:
        conn.execute(text("CREATE TABLE t (id INTEGER PRIMARY KEY)"))
        conn.commit()
    engine.dispose()

    db_module.write_credential_json(db_module.CONNECTIONS_FILE, {
        "legacy": {"name": "legacy", "db_type": "sqlite", "database": str(path)},
    })

    manager = DatabaseManager()
    assert manager.active_id == "legacy"
    assert manager.get_schema_metadata()["tables"]
    manager._dispose_engine()


def test_credential_write_is_atomic_and_leaves_no_temp_files(isolated_config):
    manager = DatabaseManager()
    manager.add_connection("c1", {"name": "c1", "db_type": "sqlite", "path": "x.db"})

    leftovers = [n for n in os.listdir(isolated_config) if n != "connections.json"]
    assert leftovers == []


def test_interrupted_write_leaves_the_previous_file_intact(isolated_config, monkeypatch):
    path = str(isolated_config / "connections.json")
    db_module.write_credential_json(path, {"good": {"db_type": "sqlite"}})

    def exploding_dump(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(db_module.json, "dump", exploding_dump)
    with pytest.raises(OSError):
        db_module.write_credential_json(path, {"bad": {}})

    with open(path) as f:
        assert json.load(f) == {"good": {"db_type": "sqlite"}}
    # The failed temp file was cleaned up.
    assert os.listdir(isolated_config) == ["connections.json"]
