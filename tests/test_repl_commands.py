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
from schemapilot.names import (
    bare_name,
    completion_candidates,
    match_table_names,
    normalise,
    resolve_table_name,
)
from schemapilot.rendering import PREVIEW_ROWS
from schemapilot.repl.commands import (
    COMMANDS,
    COMMAND_ORDER,
    SQL_PREVIEW_ROWS,
    _render_result,
    dispatch,
    handle_line,
    suggest,
)
from schemapilot.repl.completion import SchemaPilotCompleter
from schemapilot.repl.session import (
    NO_CONNECTION_ACTION,
    NO_CONNECTION_HEADLINE,
    PROVIDER_API_KEY_ENV,
    ReplSession,
)


# --------------------------------------------------------------------------- fixtures


class _StubModelManager:
    """Stands in for ModelProfileManager so no models.json or API key is involved."""

    active_id = "stub"

    @staticmethod
    def get_active_profile():
        return {"provider": "google", "model_name": "stub-model", "api_key": "", "base_url": ""}


@pytest.fixture
def session(tmp_path, isolated_config, seeded_sqlite_path):
    """A ReplSession on a SQLite profile, rendering into a recordable console."""
    primary = seeded_sqlite_path

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


@pytest.mark.parametrize(
    "statement",
    [
        "PRAGMA user_version = 4242",
        "PRAGMA writable_schema = ON",
        "PRAGMA journal_mode = WAL",
    ],
)
def test_sql_refuses_writable_pragmas(session, statement):
    """Regression at the REPL surface: a writable PRAGMA persists on file-backed SQLite.

    A class-wide ``exp.Pragma`` allowance used to let these through, so `/sql` -- a read-only
    surface -- could permanently rewrite the database header. Asserted here as well as in the
    sentry's own suite because `/sql` is the surface that has to stay refused: it is the one path
    where the user's raw text reaches the engine with no LLM in between.
    """
    handle_line(session, f"/sql {statement}")
    text_out = output(session)
    assert "Security Violation" in text_out
    assert "read-only" in text_out


def test_writable_pragma_does_not_reach_the_database(session, monkeypatch):
    """The refusal happens BEFORE execution, not as a post-hoc complaint."""
    def explode(*args, **kwargs):
        raise AssertionError("a refused statement must never be executed")

    monkeypatch.setattr(session.db, "execute_query", explode)
    handle_line(session, "/sql PRAGMA user_version = 4242")
    assert "Security Violation" in output(session)


def test_sql_still_allows_a_bare_readonly_pragma(session):
    """The fix is by-name gating, not a blanket PRAGMA ban: introspection pragmas still work."""
    handle_line(session, "/sql PRAGMA database_list")
    assert "Security Violation" not in output(session)


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


def test_why_says_nothing_is_recorded_before_the_first_question(session):
    """Two legitimate causes: no question yet, or a question that died before pruning ran. The
    message must cover both without claiming pruning is missing -- it has landed."""
    handle_line(session, "/why")
    text_out = output(session)
    assert "No table-selection decision recorded" in text_out
    assert "before table selection ran" in text_out
    # Pruning is real now, so /why must not tell the user it is absent from the build.
    assert "not yet enabled" not in text_out


def test_why_reports_a_recorded_selection(session):
    session.last_selection = {"tables": ["orders", "customers"], "scores": {"orders": 0.91}}
    handle_line(session, "/why")
    text_out = output(session)
    assert "orders" in text_out
    assert "0.91" in text_out


def test_why_reports_the_reason_pruning_recorded_per_table(session):
    """A score alone does not explain a selection: a pinned table scores 0.00 and is still sent."""
    session.last_selection = {
        "tables": ["orders", "customers"],
        "scores": {"orders": 0.0, "customers": 1.4},
        "reasons": {"orders": "pinned with @", "customers": "lexical match (score 1)"},
        "strategy": "pruned",
    }
    handle_line(session, "/why")
    text_out = output(session)
    assert "pinned with @" in text_out
    assert "lexical match" in text_out


@pytest.mark.parametrize(
    "strategy,expected",
    [
        ("pruned", "only the tables below were sent"),
        ("full-catalog", "every table was sent and nothing was pruned"),
        ("empty-catalog", "catalog was empty"),
    ],
)
def test_why_explains_the_pruning_strategy_in_words(session, strategy, expected):
    """`full-catalog` printed raw reads like "pruning is off"; it means "nothing needed pruning"."""
    session.last_selection = {
        "tables": ["orders"],
        "scores": {"orders": 0.5},
        "reasons": {},
        "strategy": strategy,
    }
    handle_line(session, "/why")
    assert expected in output(session)


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


def _install_fake_agent(monkeypatch, events, record, legacy_signature=False):
    """Substitutes schemapilot.agent with a stub module.

    Injected through sys.modules rather than monkeypatching the class so the test never imports
    langchain and never makes an LLM call.

    ``legacy_signature=True`` drops ``pinned_tables``/``catalog`` from ``execute``, standing in
    for an agent build that predates them -- the session feature-checks the signature rather than
    assuming, and that path needs covering too.
    """
    import sys
    import types

    class FakeAgent:
        async def execute(self, question, history=None, llm_config=None,
                          pinned_tables=None, catalog=None):
            record["question"] = question
            record["pinned_tables"] = pinned_tables
            record["catalog"] = catalog
            for event in events:
                yield event

    class LegacyAgent:
        async def execute(self, question, history=None, llm_config=None):
            record["question"] = question
            for event in events:
                yield event

    module = types.ModuleType("schemapilot.agent")
    module.SchemaPilotAgent = LegacyAgent if legacy_signature else FakeAgent
    monkeypatch.setitem(sys.modules, "schemapilot.agent", module)


def test_schema_selection_event_is_captured_into_last_selection(session, monkeypatch):
    """The agent's pruning event (T6) is stored, not printed -- /why is where it surfaces."""
    record = {}
    _install_fake_agent(
        monkeypatch,
        ['{"event": "schema_selection", "tables": ["orders"], "scores": {"orders": 0.5}, '
         '"reasons": {"orders": "pinned with @"}, "strategy": "pruned"}\n'],
        record,
    )

    session.catalog.warm()
    session.pin_table("orders")
    session.ask("who spends the most?")

    assert session.last_selection == {
        "tables": ["orders"],
        "scores": {"orders": 0.5},
        "reasons": {"orders": "pinned with @"},
        "strategy": "pruned",
        "pin_problems": [],
    }
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


# ------------------------------------------------------- the warmed catalog reaches the agent


def test_question_after_use_does_not_introspect_again(session, second_connection, monkeypatch):
    """/use warms the catalog once; the question must reuse it, not re-introspect.

    This is the "SchemaCache is the sole owner of metadata" contract in its most expensive form:
    re-fetching per question duplicates slow warehouse introspection and lets the LLM prompt
    describe a different catalog than completion and /schema do.
    """
    record = {}
    _install_fake_agent(monkeypatch, ['{"event": "final_output", "text": "ok"}\n'], record)

    handle_line(session, "/use warehouse")
    assert session.catalog.is_warm

    # Anything reaching the database after the warm is a duplicate fetch, so make it fatal.
    def explode(*args, **kwargs):
        raise AssertionError("the question must not re-introspect after /use warmed the catalog")

    monkeypatch.setattr(session.db, "get_schema_metadata", explode)
    session.ask("how many events?")

    # Handed over verbatim as the raw get_schema_metadata() dict, not a SchemaCache wrapper.
    assert isinstance(record["catalog"], dict)
    assert set(record["catalog"]) >= {"tables", "relationships", "truncated", "total_tables"}
    assert any(name.endswith("events") for name in record["catalog"]["tables"])


def test_question_warms_the_catalog_exactly_once_across_several_questions(session, monkeypatch):
    """A cold session fetches on the first question and never again."""
    record = {}
    _install_fake_agent(monkeypatch, ['{"event": "final_output", "text": "ok"}\n'], record)

    real = session.db.get_schema_metadata
    calls = []

    def counting(*args, **kwargs):
        calls.append(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(session.db, "get_schema_metadata", counting)
    session.catalog.invalidate()

    session.ask("first question")
    session.ask("second question")
    session.ask("third question")

    assert len(calls) == 1
    assert record["catalog"]["tables"]


def test_agent_gets_the_same_catalog_object_the_completer_sees(session, monkeypatch):
    """One catalog, one owner: the tables in the prompt are the tables that complete."""
    record = {}
    _install_fake_agent(monkeypatch, ['{"event": "final_output", "text": "ok"}\n'], record)

    session.catalog.warm()
    session.ask("who spends the most?")

    assert sorted(record["catalog"]["tables"]) == session.catalog.table_names()


def test_catalog_is_not_passed_to_an_agent_that_cannot_accept_it(session, monkeypatch):
    """Feature-checked, not assumed: an older agent build must not get a TypeError."""
    record = {}
    _install_fake_agent(
        monkeypatch,
        ['{"event": "final_output", "text": "ok"}\n'],
        record,
        legacy_signature=True,
    )

    session.catalog.warm()
    session.pin_table("orders")
    session.ask("who spends the most?")

    assert record["question"] == "who spends the most?"
    assert "catalog" not in record
    assert "No table-selection decision recorded" not in output(session)


def test_a_failed_warm_omits_the_catalog_rather_than_sending_an_empty_one(session, monkeypatch):
    """A supplied catalog is trusted absolutely, so a broken warm must NOT hand over {}.

    The agent reports "No tables detected" for an empty supplied catalog, which would turn a
    transient read failure into a confident wrong answer. Omitting the kwarg lets the agent's own
    introspection surface the real error instead.
    """
    record = {}
    _install_fake_agent(monkeypatch, ['{"event": "final_output", "text": "ok"}\n'], record)

    session.catalog.invalidate()
    monkeypatch.setattr(
        session.db,
        "get_schema_metadata",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("reflection exploded")),
    )

    session.ask("how many orders?")

    assert record["catalog"] is None


def test_one_shot_cli_path_passes_no_catalog(session):
    """`schemapilot "question"` runs through cli.execute_query, which has no warmed session --
    that path must keep working by letting the agent fetch the catalog itself."""
    import inspect as _inspect

    from schemapilot.cli import SchemaPilotCLI

    source = _inspect.getsource(SchemaPilotCLI.execute_query)
    assert "catalog=" not in source


def test_status_line_advertises_read_only(session):
    assert " · ro]" in session.status_line()


# ------------------------------------------------------------- one name resolver, one policy


def _AMBIGUOUS():
    """Two schemas holding a table of the same leaf name -- the case the three old resolvers
    each answered differently."""
    return ["archive.orders", "sales.orders", "sales.customers"]


def test_bare_name_splits_on_the_last_dot():
    """Correct for 1-, 2- and 3-part names alike."""
    assert bare_name("tpch.tiny.orders") == "orders"
    assert bare_name("sales.orders") == "orders"
    assert bare_name("orders") == "orders"


@pytest.mark.parametrize("typed", ['"orders"', "`orders`", "@orders", "  ORDERS  ", "[orders]"])
def test_normalise_strips_quotes_at_signs_and_case(typed):
    assert normalise(typed) == "orders"


def test_exact_match_wins_over_looser_tiers():
    """A fully-qualified name must never be diluted by suffix or leaf matches."""
    assert match_table_names("sales.orders", _AMBIGUOUS()) == ["sales.orders"]
    assert resolve_table_name("sales.orders", _AMBIGUOUS()) == "sales.orders"


def test_dotted_suffix_resolves_a_partially_qualified_name():
    assert resolve_table_name("tiny.orders", ["tpch.tiny.orders", "tpch.sf1.lineitem"]) == "tpch.tiny.orders"


def test_leaf_name_resolves_when_it_is_unique():
    assert resolve_table_name("customers", _AMBIGUOUS()) == "sales.customers"


def test_ambiguity_never_resolves_silently():
    """The whole point of this module. Alphabetically-first would return archive.orders -- the
    stale copy -- and pruning's old behaviour dropped the pin with no explanation."""
    assert resolve_table_name("orders", _AMBIGUOUS()) is None
    # ...but the collision is retrievable, so a caller can name both tables.
    assert match_table_names("orders", _AMBIGUOUS()) == ["archive.orders", "sales.orders"]


def test_an_unknown_name_matches_nothing():
    assert match_table_names("nope", _AMBIGUOUS()) == []
    assert resolve_table_name("nope", _AMBIGUOUS()) is None
    assert resolve_table_name("", _AMBIGUOUS()) is None


def test_completion_matches_prefixes_of_either_the_qualified_or_the_leaf_name():
    """Distinct from resolution on purpose: completion works on a half-typed word."""
    assert completion_candidates("ord", _AMBIGUOUS()) == ["archive.orders", "sales.orders"]
    assert completion_candidates("sales.", _AMBIGUOUS()) == ["sales.customers", "sales.orders"]
    assert completion_candidates("", _AMBIGUOUS()) == sorted(_AMBIGUOUS())


def test_the_completer_and_the_cache_agree_about_an_ambiguous_name(session, monkeypatch):
    """The bug this replaced: `@orders` completed, resolved to a DIFFERENT table for /schema, and
    was then dropped before it reached the prompt -- three answers to one question."""
    monkeypatch.setattr(session.catalog, "table_names", lambda: _AMBIGUOUS())
    monkeypatch.setattr(type(session.catalog), "is_warm", property(lambda self: True))

    completer = SchemaPilotCompleter(session)
    completions = [c.text for c in completer.get_completions(Document("count @ord"), None)]

    # Completion offers both, so the user can see there is a choice to make...
    assert completions == ["archive.orders", "sales.orders"]
    # ...and neither the cache nor the pin picks one arbitrarily.
    assert session.catalog.resolve("orders") is None
    assert session.catalog.candidates("orders") == ["archive.orders", "sales.orders"]


def test_an_ambiguous_completion_pins_nothing(session, monkeypatch):
    """Only an unambiguous single candidate is pinned; guessing here would silently steer the
    prompt at the wrong schema."""
    monkeypatch.setattr(session.catalog, "table_names", lambda: _AMBIGUOUS())
    monkeypatch.setattr(type(session.catalog), "is_warm", property(lambda self: True))

    completer = SchemaPilotCompleter(session)
    list(completer.get_completions(Document("count @ord"), None))
    assert session.pinned_tables == []


def test_schema_on_an_ambiguous_name_refuses_instead_of_guessing(session, monkeypatch):
    monkeypatch.setattr(session.catalog, "table_names", lambda: _AMBIGUOUS())
    monkeypatch.setattr(type(session.catalog), "is_warm", property(lambda self: True))

    handle_line(session, "/schema orders")
    text_out = output(session)
    assert "archive.orders" in text_out
    assert "sales.orders" in text_out


def test_a_cold_cache_resolves_nothing(session):
    """Resolution reads cached state only; a cold cache must not look like an empty database."""
    session.catalog.invalidate()
    assert session.catalog.resolve("orders") is None
    assert session.catalog.candidates("orders") == []


# ------------------------------------------------------------- one escaped row renderer


BRACKETED_ROWS = {
    "columns": ["label", "payload"],
    # Real database content that Rich reads as markup: a Postgres array literal, an array type
    # name, and a product literally called [legacy].
    "rows": [{"label": "[legacy]", "payload": "[1,2,3]"}],
}


def test_result_cells_containing_brackets_are_not_eaten_as_markup(session):
    """Rich deletes unrecognised style tags, so an unescaped cell shows the user a WRONG value
    with nothing to indicate text went missing."""
    _render_result(session, BRACKETED_ROWS)
    text_out = output(session)
    assert "[legacy]" in text_out
    assert "[1,2,3]" in text_out


def test_the_one_shot_cli_path_escapes_result_cells_too(session, tmp_path):
    """The bug: /sql escaped its cells but `schemapilot "question"` did not, so the same value
    printed correctly in the REPL and corrupted on the command line."""
    from schemapilot.cli import SchemaPilotCLI

    cli = SchemaPilotCLI.__new__(SchemaPilotCLI)
    cli.history = []
    cli.console = Console(record=True, width=140, force_terminal=False, no_color=True)

    cli.handle_event({
        "event": "swarm_completed",
        "sql": "SELECT 1",
        "summary": "done",
        "raw_data": BRACKETED_ROWS,
    })
    text_out = cli.console.export_text()
    assert "[legacy]" in text_out
    assert "[1,2,3]" in text_out


def test_agent_messages_and_errors_are_escaped_on_the_one_shot_path(session):
    """Losing half an error message is how a user fixes the wrong thing: install_hint() returns
    `pip install 'schemapilot[trino]'` and Rich would delete the `[trino]`."""
    from schemapilot.cli import SchemaPilotCLI

    cli = SchemaPilotCLI.__new__(SchemaPilotCLI)
    cli.history = []
    cli.console = Console(record=True, width=200, force_terminal=False, no_color=True)

    cli.handle_event({"event": "error", "error": "install pip install 'schemapilot[trino]'"})
    cli.handle_event({"event": "agent_message", "agent": "Architect", "message": "reading col[0]"})
    text_out = cli.console.export_text()
    assert "schemapilot[trino]" in text_out
    assert "col[0]" in text_out


def test_both_surfaces_truncate_at_the_same_row(session):
    """Two renderers with caps of 15 and 25 meant the same result printed differently depending
    on which surface you asked from -- a difference with no meaning behind it."""
    assert SQL_PREVIEW_ROWS == PREVIEW_ROWS

    rows = [{"n": i} for i in range(PREVIEW_ROWS + 5)]
    _render_result(session, {"columns": ["n"], "rows": rows})
    text_out = output(session)
    assert f"Showing {PREVIEW_ROWS} of {len(rows)} rows" in text_out
    assert str(PREVIEW_ROWS - 1) in text_out
    # The row just past the cap is not printed.
    assert f"| {PREVIEW_ROWS} " not in text_out


def test_a_payload_with_no_rows_key_prints_the_drivers_message_escaped(session):
    """Statements returning no result set come back as {"message": ...}, not a row list."""
    _render_result(session, {"message": "ok [done]"})
    assert "ok [done]" in output(session)


def test_an_empty_row_list_says_zero_rows(session):
    _render_result(session, {"columns": ["n"], "rows": []})
    assert "0 rows" in output(session)


# ------------------------------------------------------------- small consolidations


def test_a_question_with_no_connection_gives_the_same_fix_as_a_command(session):
    """The headline and its one action had three verbatim copies; they must not drift."""
    session.db.active_id = None

    session.ask("who spends the most?")
    handle_line(session, "/tables")
    described = session.describe_error(RuntimeError("No active database connection selected"))

    text_out = output(session)
    assert text_out.count(NO_CONNECTION_HEADLINE) == 2
    assert text_out.count("--list-conns") == 2
    assert described == {"headline": NO_CONNECTION_HEADLINE, "action": NO_CONNECTION_ACTION}


def test_the_provider_env_var_map_matches_what_llm_actually_reads():
    """session.py names the env var in an auth error while llm.get_llm reads it inline; llm.py
    exposes no mapping to import, so this test is the guard against the copy drifting."""
    import inspect as _inspect

    from schemapilot import llm as llm_module

    source = _inspect.getsource(llm_module.get_llm)
    for provider, env_var in PROVIDER_API_KEY_ENV.items():
        assert env_var in source, f"{env_var} is claimed for '{provider}' but llm.py never reads it"
        assert provider in source

    # And the reverse: a provider key added to llm.py must be added here too, or an auth error
    # will send the user to set LLM_API_KEY when the code reads something else.
    import re

    read_vars = set(re.findall(r'os\.getenv\("([A-Z_]*API_KEY)"', source))
    assert read_vars - set(PROVIDER_API_KEY_ENV.values()) <= {"LLM_API_KEY"}


def test_an_llm_auth_error_names_the_variable_for_the_active_provider(session):
    described = session.describe_error(ValueError("OpenAI API key is missing"))
    assert "GOOGLE_API_KEY" in described["action"]  # the stub profile's provider


def test_the_repl_log_lives_in_the_shared_config_dir():
    """USER_CONFIG_DIR had three independent definitions; a REPL log under a different one is a
    log the rest of the CLI does not look for."""
    from schemapilot import paths
    from schemapilot.repl import session as session_module

    assert session_module.USER_CONFIG_DIR is paths.USER_CONFIG_DIR
    assert session_module.LOG_FILE.startswith(paths.USER_CONFIG_DIR)
