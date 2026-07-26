"""Shared pytest fixtures.

Unit tests need nothing from here. The integration matrix
(``tests/integration/test_engine_matrix.py``) needs three things, all provided below:

1. a hard gate -- everything under ``tests/integration/`` is skipped unless
   ``SCHEMAPILOT_IT=1``, so ``make test`` stays hermetic and offline;
2. a connection-config dict per engine, matching the shape ``db.add_connection()`` stores
   (``db_type``/``host``/``port``/``username``/``password``/``database`` plus ``catalog``,
   ``schema`` and ``path`` where the engine wants them);
3. reachability probes that SKIP -- never fail -- when a service is not up, plus a retry
   helper for Trino, whose coordinator answers HTTP long before it accepts queries
   (30-60s cold start).

Bring the servers up with ``docker compose up -d --wait``.
"""

import importlib
import json
import os
import socket
import sqlite3
import time
import urllib.error
import urllib.request

import pytest

# --------------------------------------------------------------------------- gate

IT_ENV_VAR = "SCHEMAPILOT_IT"


def integration_enabled() -> bool:
    return os.environ.get(IT_ENV_VAR, "") == "1"


def pytest_collection_modifyitems(config, items):
    """Skip the whole integration directory unless SCHEMAPILOT_IT=1.

    Done by path rather than by marker so a new integration module cannot forget the gate.
    """
    if integration_enabled():
        return
    skip = pytest.mark.skip(reason=f"integration test: set {IT_ENV_VAR}=1 (needs docker compose up -d --wait)")
    for item in items:
        if f"tests{os.sep}integration{os.sep}" in str(item.fspath) or "/tests/integration/" in str(item.fspath):
            item.add_marker(skip)


# --------------------------------------------------------------------------- probes

DEFAULT_HOST = os.environ.get("SCHEMAPILOT_IT_HOST", "127.0.0.1")


def _port(env_name: str, default: int) -> int:
    return int(os.environ.get(env_name, default))


PG_PORT = _port("SCHEMAPILOT_IT_PG_PORT", 5432)
MYSQL_PORT = _port("SCHEMAPILOT_IT_MYSQL_PORT", 3307)
TRINO_PORT = _port("SCHEMAPILOT_IT_TRINO_PORT", 8080)
CLICKHOUSE_PORT = _port("SCHEMAPILOT_IT_CLICKHOUSE_PORT", 8123)

DB_NAME = "schemapilot_db"
DB_USER = "schemapilot_user"
DB_PASSWORD = "schemapilot_password_xyz"


def tcp_open(host: str, port: int, timeout: float = 1.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def require_tcp(host: str, port: int, service: str, timeout: float = 5.0) -> None:
    """Skip (never fail) when a container port is not accepting connections.

    The budget is short on purpose: ``docker compose up -d --wait`` is the documented way to
    start the sandbox, so a closed port means "not running", not "still booting". Readiness
    *behind* an open port is what wait_for_trino/wait_for_clickhouse handle.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if tcp_open(host, port):
            return
        time.sleep(1.0)
    pytest.skip(f"{service} not reachable at {host}:{port} -- run `docker compose up -d --wait`")


def _http_get(url: str, timeout: float = 5.0) -> str:
    with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310 (localhost only)
        return response.read().decode("utf-8", "replace")


def wait_for_clickhouse(host: str = DEFAULT_HOST, port: int = CLICKHOUSE_PORT, timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    last = "no attempt made"
    while time.monotonic() < deadline:
        try:
            if "Ok" in _http_get(f"http://{host}:{port}/ping"):
                return
            last = "unexpected /ping body"
        except (urllib.error.URLError, OSError) as exc:
            last = str(exc)
        time.sleep(1.0)
    pytest.skip(f"ClickHouse not ready at {host}:{port} ({last}) -- run `docker compose up -d --wait`")


def wait_for_trino(host: str = DEFAULT_HOST, port: int = TRINO_PORT, timeout: float = None) -> None:
    """Wait for the Trino coordinator to finish starting.

    ``/v1/info`` responds while the server is still booting, with ``"starting": true``; queries
    fail with ``SERVER_STARTING_UP`` until it flips. A cold ``trinodb/trino:468`` start is
    30-60s, so the default budget is deliberately generous and overridable with
    ``SCHEMAPILOT_IT_TRINO_TIMEOUT``.
    """
    if timeout is None:
        timeout = float(os.environ.get("SCHEMAPILOT_IT_TRINO_TIMEOUT", "180"))
    deadline = time.monotonic() + timeout
    last = "no attempt made"
    while time.monotonic() < deadline:
        try:
            info = json.loads(_http_get(f"http://{host}:{port}/v1/info"))
            if info.get("starting") is False:
                return
            last = f"still starting (uptime {info.get('uptime')})"
        except (urllib.error.URLError, OSError, ValueError) as exc:
            last = str(exc)
        time.sleep(2.0)
    pytest.skip(f"Trino not ready at {host}:{port} ({last}) -- run `docker compose up -d --wait`")


# --------------------------------------------------------------------------- seed schema

#: Same customers/orders shape as tests/fixtures/*-init.sql, for the in-process engines.
SEED_STATEMENTS = (
    """
    CREATE TABLE customers (
        id      INTEGER      PRIMARY KEY,
        name    VARCHAR(100) NOT NULL,
        country VARCHAR(2)   NOT NULL
    )
    """,
    """
    CREATE TABLE orders (
        id          INTEGER       PRIMARY KEY,
        customer_id INTEGER       NOT NULL REFERENCES customers (id),
        amount      DECIMAL(10,2) NOT NULL,
        status      VARCHAR(20)   NOT NULL
    )
    """,
    "INSERT INTO customers (id, name, country) VALUES "
    "(1, 'Ada Lovelace', 'GB'), (2, 'Grace Hopper', 'US'), (3, 'Kathleen Booth', 'GB')",
    "INSERT INTO orders (id, customer_id, amount, status) VALUES "
    "(1, 1, 120.50, 'shipped'), (2, 1, 75.00, 'pending'), "
    "(3, 2, 310.25, 'shipped'), (4, 3, 42.00, 'cancelled')",
)


# --------------------------------------------------------------------------- config fixtures


@pytest.fixture
def seeded_sqlite_path(tmp_path):
    """A file-backed SQLite file carrying the shared seed schema (customers + orders, PK + FK).

    Replaces the ``_seed_sqlite`` helper that was copy-pasted between test modules; the fixture
    is always torn down with ``tmp_path``, so no cleanup is needed.
    """
    import sqlite3

    path = str(tmp_path / "seed.db")
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        for statement in SEED_STATEMENTS:
            conn.execute(statement)
        conn.commit()
    finally:
        conn.close()
    return path


@pytest.fixture
def isolated_config(tmp_path, monkeypatch):
    """Points every credential store at a throwaway HOME.

    ``schemapilot.paths`` is the single source of truth for ``USER_CONFIG_DIR``;
    ``db``/``llm`` still re-export module-level aliases for backwards-compatible
    monkeypatching, and ``repl.session`` has now been switched (build-t5-repl) so
    this fixture covers all four.
    """
    from schemapilot import db as db_module
    from schemapilot import llm as llm_module
    from schemapilot import paths as paths_module
    from schemapilot import repl

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    config_dir = home / ".config" / "schemapilot"

    monkeypatch.setattr(paths_module, "USER_CONFIG_DIR", str(config_dir))
    monkeypatch.setattr(db_module, "USER_CONFIG_DIR", str(config_dir))
    monkeypatch.setattr(db_module, "CONNECTIONS_FILE", str(config_dir / "connections.json"))
    monkeypatch.setattr(llm_module, "USER_CONFIG_DIR", str(config_dir))
    monkeypatch.setattr(llm_module, "MODELS_FILE", str(config_dir / "models.json"))
    monkeypatch.setattr(repl.session, "USER_CONFIG_DIR", str(config_dir))
    return config_dir


@pytest.fixture
def sqlite_db(tmp_path, isolated_config):
    """A SQLite database with a primary key and a foreign key, wired as the active connection."""
    from sqlalchemy import create_engine, text
    from schemapilot.db import DatabaseManager

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


# --------------------------------------------------------------------------- originals (session-scoped)


@pytest.fixture(scope="session")
def sqlite_seeded_path(tmp_path_factory) -> str:
    """A file-backed SQLite database carrying the shared seed schema (PK + real FK)."""
    path = str(tmp_path_factory.mktemp("sqlite") / "schemapilot_it.db")
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        for statement in SEED_STATEMENTS:
            conn.execute(statement)
        conn.commit()
    finally:
        conn.close()
    return path


@pytest.fixture(scope="session")
def duckdb_seeded_path(tmp_path_factory) -> str:
    """A file-backed DuckDB database carrying the shared seed schema.

    File-backed rather than ``:memory:`` because a separate SchemaPilot engine has to open the
    same database afterwards. DuckDB accepts PK/FK declarations but does not report primary
    keys through reflection, so the matrix asserts tables/columns only.
    """
    duckdb = pytest.importorskip("duckdb", reason="duckdb not installed")
    path = str(tmp_path_factory.mktemp("duckdb") / "schemapilot_it.duckdb")
    conn = duckdb.connect(path)
    try:
        for statement in SEED_STATEMENTS:
            conn.execute(statement)
    finally:
        conn.close()
    return path


@pytest.fixture(scope="session")
def sqlite_config(sqlite_seeded_path):
    return {"db_type": "sqlite", "path": sqlite_seeded_path}


@pytest.fixture(scope="session")
def duckdb_config(duckdb_seeded_path):
    return {"db_type": "duckdb", "path": duckdb_seeded_path}


@pytest.fixture(scope="session")
def postgres_config():
    require_tcp(DEFAULT_HOST, PG_PORT, "PostgreSQL")
    return {
        "db_type": "postgres",
        "host": DEFAULT_HOST,
        "port": PG_PORT,
        "username": DB_USER,
        "password": DB_PASSWORD,
        "database": DB_NAME,
        "schema": "public",
    }


@pytest.fixture(scope="session")
def mysql_config():
    require_tcp(DEFAULT_HOST, MYSQL_PORT, "MySQL")
    return {
        "db_type": "mysql",
        "host": DEFAULT_HOST,
        "port": MYSQL_PORT,
        "username": DB_USER,
        "password": DB_PASSWORD,
        "database": DB_NAME,
    }


@pytest.fixture(scope="session")
def clickhouse_config():
    require_tcp(DEFAULT_HOST, CLICKHOUSE_PORT, "ClickHouse")
    wait_for_clickhouse()
    return {
        "db_type": "clickhouse",
        "host": DEFAULT_HOST,
        "port": CLICKHOUSE_PORT,
        "username": DB_USER,
        "password": DB_PASSWORD,
        "database": DB_NAME,
    }


@pytest.fixture(scope="session")
def trino_config():
    require_tcp(DEFAULT_HOST, TRINO_PORT, "Trino")
    wait_for_trino()
    return {
        # No seeding: the built-in tpch catalog supplies real 3-part names.
        "db_type": "trino",
        "host": DEFAULT_HOST,
        "port": TRINO_PORT,
        "username": "schemapilot",
        "password": "",
        "catalog": "tpch",
        "schema": "tiny",
        "http_scheme": "http",
    }


# --------------------------------------------------------------------------- manager


@pytest.fixture(scope="session")
def isolated_db_module(tmp_path_factory):
    """``schemapilot.db`` with its config directory redirected into a temp HOME.

    Integration tests add connection profiles, and profiles are persisted; without this they
    would scribble on the developer's real ``~/.config/schemapilot/connections.json``. The
    module resolves its paths at import time, so the env has to be set before a reload.
    """
    home = tmp_path_factory.mktemp("home")
    patch = pytest.MonkeyPatch()
    patch.setenv("HOME", str(home))
    patch.setenv("USERPROFILE", str(home))
    patch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    try:
        db_module = importlib.import_module("schemapilot.db")
        db_module = importlib.reload(db_module)
        yield db_module
    finally:
        patch.undo()


@pytest.fixture(scope="session")
def manager(isolated_db_module):
    """The shared :class:`DatabaseManager` used by the matrix."""
    return isolated_db_module.get_db()


@pytest.fixture
def connected(manager):
    """Return a callable that registers a config and makes it the active connection."""

    def _connect(conn_id: str, config: dict):
        manager.add_connection(conn_id, config)
        manager.select_connection(conn_id)
        return manager

    return _connect
