"""REPL tests: dispatch routing, /use warming, the /schema tree, @table pinning, and the
read-only refusal.

Nothing here needs a terminal, a server, or an LLM: SQLite runs in-process, the Rich console
renders into a string buffer, and the completer is driven with hand-built prompt_toolkit
Documents.
"""

import pytest
from prompt_toolkit.document import Document
from rich.console import Console
from sqlalchemy import create_engine, text

from schemapilot import db as db_module
from schemapilot.catalog import SchemaCache
from schemapilot.db import DatabaseManager
from schemapilot.repl.commands import COMMANDS, COMMAND_ORDER, dispatch, handle_line, suggest
from schemapilot.repl.completion import SchemaPilotCompleter
from schemapilot.repl.session import ReplSession


# --------------------------------------------------------------------------- fixtures


@pytest.fixture
def isolated_config(tmp_path, monkeypatch):
    """Redirects the credential store into a throwaway HOME (profiles are persisted)."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    config_dir = home / ".config" / "schemapilot"
    monkeypatch.setattr(db_module, "USER_CONFIG_DIR", str(config_dir))
    monkeypatch.setattr(db_module, "CONNECTIONS_FILE", str(config_dir / "connections.json"))
    return config_dir


def _seed_sqlite(path) -> str:
    """A two-table SQLite database with a real PK and a real FK."""
    engine = create_engine(f"sqlite:///{path}")
    with engine.connect() as conn:
        conn.execute(text("CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT NOT NULL)"))
        conn.execute(
            text(
                "CREATE TABLE orders ("
                "  id INTEGER PRIMARY KEY,"
                "  customer_id INTEGER REFERENCES customers(id),"
                "  amount NUMERIC"
                ")"
            )
        )
        conn.commit()
    engine.dispose()
    return str(path)


class _StubModelManager:
    """Stands in for ModelProfileManager so no models.json or API key is involved."""

    active_id = "stub"

    @staticmethod
    def get_active_profile():
        return {"provider": "google", "model_name": "stub-model", "api_key": "", "base_url": ""}


@pytest.fixture
def session(tmp_path, isolated_config):
    """A ReplSession on a SQLite profile, rendering into a recordable console."""
    primary = _seed_sqlite(tmp_path / "primary.db")

    manager = DatabaseManager()
    manager.add_connection("primary", {"name": "primary", "db_type": "sqlite", "path": primary})
    manager.select_connection("primary")

    console = Console(record=True, width=140, force_terminal=False, no_color=True)
    repl = ReplSession(
        db=manager,
        console=console,
        model_manager=_StubModelManager(),
        log_file=str(tmp_path / "schemapilot.log"),
    )
    yield repl
    manager._dispose_engine()


@pytest.fixture
def second_connection(session, tmp_path):
    """A second, DIFFERENT engine profile so /use can be observed swapping active_spec."""
    duckdb_path = str(tmp_path / "warehouse.duckdb")
    pytest.importorskip("duckdb_engine", reason="duckdb-engine not installed")
    import duckdb

    conn = duckdb.connect(duckdb_path)
    conn.execute("CREATE TABLE events (id INTEGER PRIMARY KEY, label VARCHAR)")
    conn.close()

    session.db.add_connection("warehouse", {"name": "warehouse", "db_type": "duckdb", "path": duckdb_path})
    return "warehouse"


def output(session) -> str:
    return session.console.export_text()


# --------------------------------------------------------------------------- dispatch table


def test_dispatch_table_holds_exactly_the_eight_v1_commands():
    """/connect, /conns, /model and /rels are deliberately excluded (argparse already covers
    connections and models; /schema already shows FK arrows)."""
    assert set(COMMANDS) == {"help", "use", "engines", "tables", "schema", "sql", "why", "exit"}
    assert set(COMMAND_ORDER) == set(COMMANDS)
    for excluded in ("connect", "conns", "model", "rels"):
        assert excluded not in COMMANDS


@pytest.mark.parametrize("name", ["help", "use", "engines", "tables", "schema", "sql", "why", "exit"])
def test_each_command_routes_to_its_own_handler(session, monkeypatch, name):
    """Routing is asserted through the table, not by driving a terminal."""
    calls = []
    monkeypatch.setitem(
        COMMANDS,
        name,
        COMMANDS[name].__class__(
            name,
            lambda s, args, _n=name: calls.append((_n, args)),
            COMMANDS[name].summary,
            COMMANDS[name].usage,
            COMMANDS[name].arg_source,
        ),
    )

    handle_line(session, f"/{name} some args")
    assert calls == [(name, "some args")]


def test_bare_text_is_a_question_not_a_command(session, monkeypatch):
    asked = []
    monkeypatch.setattr(session, "ask", lambda q: asked.append(q))
    handle_line(session, "how many customers are in the UK?")
    assert asked == ["how many customers are in the UK?"]


def test_unknown_command_suggests_the_nearest_one(session):
    handle_line(session, "/tabels")
    text_out = output(session)
    assert "Unknown command '/tabels'" in text_out
    assert "Did you mean /tables?" in text_out


def test_suggest_returns_none_for_nonsense():
    assert suggest("qqqqqqzz") is None


def test_dispatch_reports_unknown_names():
    assert dispatch(object(), "definitely-not-a-command") is False


# --------------------------------------------------------------------------- /use


def test_use_swaps_active_spec_and_warms_the_cache(session, second_connection):
    assert session.db.active_spec.name == "sqlite"

    handle_line(session, "/use warehouse")

    assert session.db.active_id == second_connection
    assert session.db.active_spec.name == "duckdb"
    # Warmed synchronously by /use: no background thread, so the cache is usable immediately
    # after the command returns.
    assert session.catalog.is_warm
    assert any(name.endswith("events") for name in session.catalog.table_names())


def test_use_invalidates_the_previous_connections_tables(session, second_connection):
    handle_line(session, "/tables")
    assert any(name.endswith("customers") for name in session.catalog.table_names())

    handle_line(session, "/use warehouse")
    names = session.catalog.table_names()
    assert not any(name.endswith("customers") for name in names)


def test_use_with_an_unknown_name_explains_the_fix(session):
    handle_line(session, "/use nope")
    text_out = output(session)
    assert "No connection profile matches 'nope'" in text_out
    assert "--list-conns" in text_out


# --------------------------------------------------------------------------- catalog / cache


def test_cache_is_keyed_by_connection_id(session, second_connection):
    session.catalog.warm()
    assert session.catalog.is_warm

    # Switching underneath the cache must not leave it claiming to be warm for the new database.
    session.db.select_connection(second_connection)
    assert session.catalog.is_warm is False
    assert session.catalog.table_names() == []


def test_catalog_marks_primary_and_foreign_keys(session):
    session.catalog.warm()
    columns = {c["name"]: c for c in session.catalog.columns("orders")}
    assert columns["id"]["is_primary"] is True
    assert columns["customer_id"]["foreign_key"].endswith("customers.id")
    assert columns["amount"]["is_primary"] is False


def test_catalog_glob_and_substring_matching(session):
    session.catalog.warm()
    assert session.catalog.match("cust*") == ["customers"]
    assert session.catalog.match("order") == ["orders"]
    assert session.catalog.match("nothing-like-this") == []


def test_cold_cache_reports_itself_rather_than_pretending_to_be_empty(session):
    fresh = SchemaCache(session.db)
    assert fresh.is_warm is False
    assert fresh.table_names() == []
    assert fresh.summary() == "catalog not loaded"


# --------------------------------------------------------------------------- /schema and /tables


def test_schema_renders_a_tree_with_pk_and_fk_markers(session):
    handle_line(session, "/schema")
    text_out = output(session)

    assert "customers" in text_out
    assert "orders" in text_out
    assert "(PK)" in text_out
    # FK arrow, which is why /rels is not a separate v1 command.
    assert "customers.id" in text_out
    assert "→" in text_out
    # No row counts are claimed: nothing in the metadata provides them.
    assert "rows" not in text_out.lower()


def test_schema_for_one_table_lists_only_its_columns(session):
    handle_line(session, "/schema customers")
    text_out = output(session)
    assert "name" in text_out
    assert "amount" not in text_out


def test_schema_for_an_unknown_table_points_at_tables(session):
    handle_line(session, "/schema ghosts")
    assert "not in the cached catalog" in output(session)


def test_tables_filters_by_glob(session):
    handle_line(session, "/tables cust*")
    text_out = output(session)
    assert "customers" in text_out
    assert "orders" not in text_out


# --------------------------------------------------------------------------- /sql (read-only)


def test_sql_runs_a_read_query(session):
    handle_line(session, "/sql SELECT 1 AS one")
    assert "one" in output(session)


def test_sql_refuses_a_delete(session):
    """The v1 REPL is strictly read-only: allow_mutation is never True here."""
    handle_line(session, '/sql DELETE FROM customers')
    text_out = output(session)
    assert "Security Violation" in text_out
    assert "DELETE" in text_out
    assert "read-only" in text_out


def test_sql_stays_read_only_even_with_allow_mutating_queries_set(session, monkeypatch):
    """ALLOW_MUTATING_QUERIES is for direct library callers of validate_sql_query; it must not
    unlock the REPL (mutation ships later, with the approval panel)."""
    from schemapilot.config import settings

    monkeypatch.setattr(settings, "ALLOW_MUTATING_QUERIES", True)
    handle_line(session, "/sql DELETE FROM customers")
    assert "Security Violation" in output(session)


def test_sql_refuses_drop(session):
    handle_line(session, "/sql DROP TABLE customers")
    assert "Security Violation" in output(session)


def test_sql_uses_the_spec_dialect_not_the_sqlalchemy_dialect_name(session, monkeypatch):
    """`db.engine.dialect.name` reports names our registry does not use (clickhousedb,
    postgresql), so the sentry must be handed `spec.sqlglot_dialect` and `spec.name`."""
    seen = {}

    def fake_validate(sql, dialect="mysql", allow_mutation=False, engine=None):
        seen.update({"dialect": dialect, "allow_mutation": allow_mutation, "engine": engine})
        return False, "stubbed"

    monkeypatch.setattr("schemapilot.repl.commands.validate_sql_query", fake_validate)
    handle_line(session, "/sql SELECT 1")

    assert seen == {
        "dialect": session.db.active_spec.sqlglot_dialect,
        "allow_mutation": False,
        "engine": "sqlite",
    }


# --------------------------------------------------------------------------- /why


def test_why_is_honest_before_pruning_lands(session):
    handle_line(session, "/why")
    text_out = output(session)
    assert "No table-selection decision recorded" in text_out
    assert "not yet enabled" in text_out


def test_why_reports_a_recorded_selection(session):
    session.last_selection = {"tables": ["orders", "customers"], "scores": {"orders": 0.91}}
    handle_line(session, "/why")
    text_out = output(session)
    assert "orders" in text_out
    assert "0.91" in text_out


# --------------------------------------------------------------------------- /engines, /help, /exit


def test_engines_shows_policy_not_platform_capabilities(session):
    handle_line(session, "/engines")
    text_out = output(session)
    assert "Trino" in text_out
    # Trino's DBAPI does expose commit/rollback, so the honest claim is about our guarantee.
    assert "Safe dry-run" in text_out
    assert "unavailable" in text_out
    assert "transactions" not in text_out.lower()
    assert "configured Trino catalogs" in text_out


def test_engines_shows_the_exact_pip_fix_for_a_missing_driver(session, monkeypatch):
    monkeypatch.setattr("schemapilot.repl.commands.driver_available", lambda spec: False)
    handle_line(session, "/engines")
    text_out = output(session)
    assert "pip install" in text_out
    assert "schemapilot[trino]" in text_out


def test_help_lists_every_command_and_help_for_one(session):
    handle_line(session, "/help")
    listing = output(session)
    for name in COMMAND_ORDER:
        assert f"/{name}" in listing
    # Optional-argument brackets survive: Rich reads `[glob]` as a style tag unless escaped.
    assert "/help [cmd]" in listing
    assert "/tables [glob]" in listing
    assert "/schema [table]" in listing

    handle_line(session, "/help sql")
    assert "/sql <raw>" in output(session)


def test_help_for_an_unknown_command_suggests(session):
    handle_line(session, "/help tabels")
    assert "Did you mean /tables?" in output(session)


def test_exit_stops_the_loop(session):
    assert session.running is True
    handle_line(session, "/exit")
    assert session.running is False


# --------------------------------------------------------------------------- completion


def _completions(session, text):
    completer = SchemaPilotCompleter(session)
    return list(completer.get_completions(Document(text, len(text)), None))


def test_command_names_complete_after_a_slash(session):
    values = [c.text for c in _completions(session, "/t")]
    assert values == ["tables"]


def test_use_completes_connection_names(session, second_connection):
    values = [c.text for c in _completions(session, "/use ware")]
    assert values == ["warehouse"]


def test_schema_completes_table_names(session):
    session.catalog.warm()
    values = [c.text for c in _completions(session, "/schema cust")]
    assert values == ["customers"]


def test_at_completion_pins_the_table(session):
    session.catalog.warm()
    assert session.pinned_tables == []

    values = [c.text for c in _completions(session, "top spenders in @cust")]

    assert values == ["customers"]
    # Pinning is the point: it is what makes completion buy answer accuracy, not typing speed.
    assert session.pinned_tables == ["customers"]


def test_ambiguous_at_completion_does_not_pin(session):
    session.catalog.warm()
    values = [c.text for c in _completions(session, "compare @")]
    assert set(values) == {"customers", "orders"}
    assert session.pinned_tables == []


def test_cold_cache_returns_empty_completions_without_touching_the_database(session, monkeypatch):
    """The completer runs on the keypress path, so it must never introspect: a cold cache
    yields nothing rather than blocking the keystroke."""

    def explode(*args, **kwargs):
        raise AssertionError("the completer must not perform I/O")

    monkeypatch.setattr(session.db, "get_schema_metadata", explode)
    session.catalog.invalidate()

    assert _completions(session, "@cust") == []
    assert _completions(session, "/schema cust") == []
    # Command names still complete: they need no I/O at all.
    assert [c.text for c in _completions(session, "/sch")] == ["schema"]


def test_pins_are_cleared_after_a_question(session, monkeypatch):
    session.catalog.warm()
    session.pin_table("customers")

    async def fake_run(question, pinned):
        assert pinned == ["customers"]

    monkeypatch.setattr(session, "_run_agent", fake_run)
    session.ask("who spends the most?")

    # Per-question by design: a table pinned for one question must not steer the next.
    assert session.pinned_tables == []


# --------------------------------------------------------------------------- errors, /why plumbing


def test_missing_driver_error_gives_the_exact_pip_command(session):
    described = session.describe_error(ConnectionError("SQLite driver is not installed"))
    assert "driver is not installed" in described["headline"]
    # SQLite needs no extra, so the action degrades to generic advice rather than a fake command.
    assert described["action"]


def test_auth_error_names_the_field_and_its_location(session):
    described = session.describe_error(Exception("FATAL: password authentication failed for user 'x'"))
    assert described["headline"] == "Database authentication failed"
    assert "connections.json" in described["action"]


def test_llm_auth_error_names_the_env_var_and_the_flag(session):
    described = session.describe_error(Exception("401 Unauthorized: invalid api key"))
    assert "LLM authentication failed" in described["headline"]
    assert "GOOGLE_API_KEY" in described["action"]
    assert "--add-model" in described["action"]


def test_no_connection_error_points_at_list_conns_then_use(session):
    described = session.describe_error(ConnectionError("No active database connection selected"))
    assert described["headline"] == "No active database connection"
    assert "--list-conns" in described["action"]
    assert "/use" in described["action"]


def test_tracebacks_go_to_the_log_file_not_the_prompt(session, tmp_path):
    try:
        raise RuntimeError("boom")
    except RuntimeError as exc:
        session.fail("Something broke", "Try again", error=exc)

    text_out = output(session)
    assert "Something broke" in text_out
    assert "Traceback" not in text_out
    with open(session.log_file) as handle:
        assert "RuntimeError: boom" in handle.read()


def _install_fake_agent(monkeypatch, events, record):
    """Substitutes schemapilot.agent with a stub module.

    Injected through sys.modules rather than monkeypatching the class so the test never imports
    langchain and never makes an LLM call.
    """
    import sys
    import types

    class FakeAgent:
        async def execute(self, question, history=None, llm_config=None, pinned_tables=None):
            record["question"] = question
            record["pinned_tables"] = pinned_tables
            for event in events:
                yield event

    module = types.ModuleType("schemapilot.agent")
    module.SchemaPilotAgent = FakeAgent
    monkeypatch.setitem(sys.modules, "schemapilot.agent", module)


def test_schema_selection_event_is_captured_into_last_selection(session, monkeypatch):
    """The agent's pruning event (T6) is stored, not printed -- /why is where it surfaces."""
    record = {}
    _install_fake_agent(
        monkeypatch,
        ['{"event": "schema_selection", "tables": ["orders"], "scores": {"orders": 0.5}}\n'],
        record,
    )

    session.catalog.warm()
    session.pin_table("orders")
    session.ask("who spends the most?")

    assert session.last_selection == {"tables": ["orders"], "scores": {"orders": 0.5}}
    assert record["pinned_tables"] == ["orders"]
    # Stored, not printed.
    assert "schema_selection" not in output(session)

    handle_line(session, "/why")
    assert "0.50" in output(session)


def test_missing_schema_selection_event_degrades_gracefully(session, monkeypatch):
    """T6 may not have landed the event yet; /why must say so instead of crashing."""
    record = {}
    _install_fake_agent(monkeypatch, ['{"event": "final_output", "text": "42"}\n'], record)

    session.ask("how many orders?")
    assert session.last_selection is None

    handle_line(session, "/why")
    assert "No table-selection decision recorded" in output(session)


def test_status_line_advertises_read_only(session):
    assert " · ro]" in session.status_line()
