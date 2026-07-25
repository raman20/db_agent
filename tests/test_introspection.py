"""Introspection, credential-hygiene and lifecycle tests for schemapilot.db.

No test here needs a server: SQLite and DuckDB run in-process, and the Trino case is pure URL
construction.
"""

import json
import os
import stat

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from schemapilot import db as db_module
from schemapilot.db import DatabaseManager


# --------------------------------------------------------------------------- fixtures


@pytest.fixture
def isolated_config(tmp_path, monkeypatch):
    """Points both credential stores at a throwaway HOME."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    config_dir = home / ".config" / "schemapilot"
    monkeypatch.setattr(db_module, "USER_CONFIG_DIR", str(config_dir))
    monkeypatch.setattr(db_module, "CONNECTIONS_FILE", str(config_dir / "connections.json"))

    from schemapilot import llm as llm_module

    monkeypatch.setattr(llm_module, "USER_CONFIG_DIR", str(config_dir))
    monkeypatch.setattr(llm_module, "MODELS_FILE", str(config_dir / "models.json"))
    return config_dir


@pytest.fixture
def sqlite_db(tmp_path, isolated_config):
    """A SQLite database with a primary key and a foreign key, wired as the active connection."""
    path = tmp_path / "pilot.db"
    engine = create_engine(f"sqlite:///{path}")
    with engine.connect() as conn:
        conn.execute(text("CREATE TABLE parent (pid INTEGER PRIMARY KEY, nm TEXT NOT NULL)"))
        conn.execute(
            text("CREATE TABLE child (cid INTEGER PRIMARY KEY, pid INTEGER REFERENCES parent(pid))")
        )
        conn.commit()
    engine.dispose()

    manager = DatabaseManager()
    manager.add_connection("sqlite-fx", {"name": "fx", "db_type": "sqlite", "path": str(path)})
    manager.select_connection("sqlite-fx")
    yield manager
    manager._dispose_engine()


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


def test_test_connection_disposes_its_throwaway_engine(isolated_config, monkeypatch, tmp_path):
    manager = DatabaseManager()
    created = []
    real_create_engine = db_module.create_engine

    def tracking_create_engine(*args, **kwargs):
        engine = real_create_engine(*args, **kwargs)
        created.append(engine)
        return engine

    monkeypatch.setattr(db_module, "create_engine", tracking_create_engine)

    ok, _ = manager.test_connection({"db_type": "sqlite", "path": str(tmp_path / "probe.db")})
    assert ok
    assert len(created) == 1
    # A disposed pool reports zero checked-out/idle connections under a new pool instance.
    assert created[0].pool.checkedin() == 0


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
