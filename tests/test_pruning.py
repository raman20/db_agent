"""Pruning and prompt-assembly tests.

No test here makes an LLM call or touches a network: the LLM is a stub that records the prompts
it was handed, and the database is a fake that records the SQL it was asked to run. That is
deliberate -- the whole point of T6 is *what goes into the prompt*, which is exactly what a stub
can assert and a live call cannot.
"""

import asyncio
import json

import pytest

from schemapilot import agent as agent_module
from schemapilot.agent import (
    SAMPLE_ROW_LIMIT,
    SchemaPilotAgent,
    build_sample_query,
    render_schema,
)
from schemapilot.config import settings
from schemapilot.engines import get_spec
from schemapilot.pruning import select_relevant_tables


# --------------------------------------------------------------------------- catalog fixtures


def _table(name, columns, pk=None, schema=None, table=None):
    """A metadata entry shaped exactly like ``get_schema_metadata()`` builds them.

    Carrying ``schema``/``table`` separately matters: those components -- not the dotted display
    key -- are what the sampling SQL is built from.
    """
    if table is None:
        schema, _, table = name.rpartition(".")
    return {
        "name": name,
        "schema": schema or None,
        "table": table,
        "columns": [
            {"name": col, "type": "INTEGER" if col.endswith("_id") or col == "id" else "VARCHAR",
             "nullable": col != (pk or "id"), "is_primary": col == (pk or "id")}
            for col in columns
        ],
    }


@pytest.fixture
def wide_catalog():
    """A 60-table catalog: a handful of meaningful tables plus filler, like a real warehouse."""
    tables = {
        "public.customers": _table("public.customers", ["id", "name", "email", "country"]),
        "public.orders": _table("public.orders", ["id", "customer_id", "placed_at", "status"]),
        "public.order_items": _table("public.order_items", ["id", "order_id", "product_id", "quantity"]),
        "public.products": _table("public.products", ["id", "title", "price", "category_id"]),
        "public.categories": _table("public.categories", ["id", "label"]),
        "public.invoices": _table("public.invoices", ["id", "order_id", "total"]),
    }
    # Filler tables with names that share no tokens with the questions under test.
    for i in range(54):
        name = f"public.zz_audit_log_{i:02d}"
        tables[name] = _table(name, ["id", "payload", "written_at"])

    relationships = [
        {"from_table": "public.orders", "from_columns": ["customer_id"],
         "to_table": "public.customers", "to_columns": ["id"]},
        {"from_table": "public.order_items", "from_columns": ["order_id"],
         "to_table": "public.orders", "to_columns": ["id"]},
        {"from_table": "public.order_items", "from_columns": ["product_id"],
         "to_table": "public.products", "to_columns": ["id"]},
        {"from_table": "public.products", "from_columns": ["category_id"],
         "to_table": "public.categories", "to_columns": ["id"]},
        {"from_table": "public.invoices", "from_columns": ["order_id"],
         "to_table": "public.orders", "to_columns": ["id"]},
    ]
    return {"tables": tables, "relationships": relationships, "truncated": False,
            "total_tables": len(tables)}


@pytest.fixture
def small_catalog():
    tables = {
        "main.customers": _table("main.customers", ["id", "name"]),
        "main.orders": _table("main.orders", ["id", "customer_id"]),
        "main.widgets": _table("main.widgets", ["id", "label"]),
    }
    return {"tables": tables,
            "relationships": [{"from_table": "main.orders", "from_columns": ["customer_id"],
                               "to_table": "main.customers", "to_columns": ["id"]}],
            "truncated": False, "total_tables": len(tables)}


# --------------------------------------------------------------------------- selection


def test_selects_the_tables_the_question_names(wide_catalog):
    selection = select_relevant_tables("how many orders did each customer place?", wide_catalog)

    assert "public.orders" in selection["tables"]
    assert "public.customers" in selection["tables"]
    # The 54 unrelated audit tables must not consume the budget.
    assert not [t for t in selection["tables"] if "zz_audit_log" in t]
    # Scores are returned for every table so /why can explain both picks and rejections.
    assert selection["scores"]["public.orders"] > selection["scores"]["public.zz_audit_log_00"]


def test_fk_one_hop_expansion_pulls_in_the_bridge_table():
    """The join bridge is included even though nothing about its name matches the question.

    ``basket_line`` shares no token with "customers"/"products" and its FK columns are named
    ``cust_ref``/``prod_ref``, so lexical scoring alone gives it nothing -- yet without it the
    join simply cannot be written.
    """
    tables = {
        "public.customers": _table("public.customers", ["id", "name"]),
        "public.products": _table("public.products", ["id", "title"]),
        "public.basket_line": _table("public.basket_line", ["id", "cust_ref", "prod_ref"]),
    }
    tables.update({f"public.filler_{i:02d}": _table(f"public.filler_{i:02d}", ["id", "blob"])
                   for i in range(20)})
    catalog = {
        "tables": tables,
        "relationships": [
            {"from_table": "public.basket_line", "from_columns": ["cust_ref"],
             "to_table": "public.customers", "to_columns": ["id"]},
            {"from_table": "public.basket_line", "from_columns": ["prod_ref"],
             "to_table": "public.products", "to_columns": ["id"]},
        ],
        "truncated": False,
        "total_tables": len(tables),
    }

    selection = select_relevant_tables("which customers bought which products?", catalog)

    assert "public.customers" in selection["tables"]
    assert "public.products" in selection["tables"]
    assert "public.basket_line" in selection["tables"], selection["reasons"]
    assert "foreign-key neighbour" in selection["reasons"]["public.basket_line"]


def test_fk_expansion_is_one_hop_only(wide_catalog):
    """From a single seed we reach its neighbours, never the whole normalised graph."""
    catalog = {
        "tables": {k: wide_catalog["tables"][k] for k in
                   ["public.categories", "public.products", "public.order_items",
                    "public.orders", "public.customers"]},
        "relationships": wide_catalog["relationships"],
        "truncated": False,
        "total_tables": 5,
    }
    # 5 tables is under the cap, so force pruning by shrinking the budget instead.
    catalog["tables"].update({f"public.filler_{i}": _table(f"public.filler_{i}", ["id"])
                              for i in range(20)})
    catalog["total_tables"] = len(catalog["tables"])

    selection = select_relevant_tables("list the categories", catalog)

    assert "public.categories" in selection["tables"]
    assert "public.products" in selection["tables"]  # one hop
    # customers is three hops from categories; it must not be dragged in.
    assert "public.customers" not in selection["tables"]


def test_cap_is_respected(wide_catalog, monkeypatch):
    selection = select_relevant_tables(
        "orders customers products categories invoices items audit log payload written", wide_catalog
    )
    assert len(selection["tables"]) <= settings.MAX_PROMPT_TABLES == 12

    monkeypatch.setattr(settings, "MAX_PROMPT_TABLES", 4)
    tighter = select_relevant_tables("orders and customers and products and invoices", wide_catalog)
    assert len(tighter["tables"]) <= 4


def test_small_database_sends_every_table(small_catalog):
    selection = select_relevant_tables("how many widgets?", small_catalog)

    assert sorted(selection["tables"]) == sorted(small_catalog["tables"])
    assert selection["strategy"] == "full-catalog"


def test_pinned_tables_are_always_included(wide_catalog):
    selection = select_relevant_tables(
        "how many orders?", wide_catalog, pinned=["public.zz_audit_log_07"]
    )
    assert "public.zz_audit_log_07" in selection["tables"]
    assert selection["reasons"]["public.zz_audit_log_07"] == "pinned with @"


def test_pinned_tables_resolve_from_an_unqualified_name(wide_catalog):
    selection = select_relevant_tables("anything", wide_catalog, pinned=["@invoices"])
    assert "public.invoices" in selection["tables"]


def test_pinned_table_survives_a_full_budget(wide_catalog, monkeypatch):
    monkeypatch.setattr(settings, "MAX_PROMPT_TABLES", 2)
    selection = select_relevant_tables(
        "orders customers products invoices categories", wide_catalog,
        pinned=["public.zz_audit_log_01"],
    )
    assert "public.zz_audit_log_01" in selection["tables"]


def test_empty_catalog_is_not_a_crash():
    selection = select_relevant_tables("anything", {"tables": {}, "relationships": []})
    assert selection["tables"] == []


# --------------------------------------------------------------------------- rendering


def test_render_is_compact_and_carries_pk_and_fk(small_catalog):
    rendered = render_schema(small_catalog, sorted(small_catalog["tables"]))

    assert "TABLE main.orders" in rendered
    assert "customer_id: INTEGER" in rendered
    assert "id: INTEGER PK" in rendered
    assert "main.orders(customer_id) -> main.customers(id)" in rendered
    assert "CREATE TABLE" not in rendered


def test_sample_query_uses_quoted_qualified_identifiers():
    pg = get_spec("postgres")
    assert build_sample_query(pg, "public.orders", _table("public.orders", ["id"])) == (
        'SELECT * FROM "public"."orders" LIMIT 3'
    )
    assert build_sample_query(get_spec("mysql"), "shop.orders", _table("shop.orders", ["id"])) == (
        "SELECT * FROM `shop`.`orders` LIMIT 3"
    )
    # An embedded quote character is escaped, never interpolated raw.
    assert build_sample_query(pg, 'public.we"ird', _table('public.we"ird', ["id"])) == (
        'SELECT * FROM "public"."we""ird" LIMIT 3'
    )


def test_sample_query_handles_identifiers_containing_dots():
    """A dot inside a name must not be read as a qualification separator.

    The display key ``sales.2024`` is ambiguous on its own -- it can mean a SQLite table called
    ``sales.2024`` or a Postgres table ``2024`` in schema ``sales``. The metadata components
    disambiguate it, and the two cases must produce DIFFERENT SQL.
    """
    sqlite_meta = _table("sales.2024", ["id"], schema=None, table="sales.2024")
    assert build_sample_query(get_spec("sqlite"), "sales.2024", sqlite_meta) == (
        'SELECT * FROM "sales.2024" LIMIT 3'
    )

    pg_meta = _table("sales.2024", ["id"], schema="sales", table="2024")
    assert build_sample_query(get_spec("postgres"), "sales.2024", pg_meta) == (
        'SELECT * FROM "sales"."2024" LIMIT 3'
    )

    # A dotted TABLE name inside a schema stays one identifier.
    dotted_in_schema = _table("public.sales.2024", ["id"], schema="public", table="sales.2024")
    assert build_sample_query(get_spec("postgres"), "public.sales.2024", dotted_in_schema) == (
        'SELECT * FROM "public"."sales.2024" LIMIT 3'
    )


def test_sample_query_keeps_the_trino_catalog_and_the_duckdb_composite_schema():
    # Trino's catalog is prefixed onto the display key by db._qualify and lives in no field, so
    # it is recovered from the key by stripping the known schema.table suffix.
    trino_meta = _table("tpch.tiny.orders", ["id"], schema="tiny", table="orders")
    assert build_sample_query(get_spec("trino"), "tpch.tiny.orders", trino_meta) == (
        'SELECT * FROM "tpch"."tiny"."orders" LIMIT 3'
    )

    # DuckDB reports a COMPOSITE schema (`db.schema`): one metadata field, two SQL identifiers.
    duck_meta = _table("dtest.other.orders", ["id"], schema="dtest.other", table="orders")
    assert build_sample_query(get_spec("duckdb"), "dtest.other.orders", duck_meta) == (
        'SELECT * FROM "dtest"."other"."orders" LIMIT 3'
    )


def test_sample_query_falls_back_to_the_key_without_components():
    """Metadata lacking split components (hand-built dicts) still yields runnable SQL."""
    assert build_sample_query(get_spec("postgres"), "public.orders", {"columns": []}) == (
        'SELECT * FROM "public"."orders" LIMIT 3'
    )


# --------------------------------------------------------------------------- stub harness


class StubLLM:
    """Records every prompt and replies with a canned SQL string / summary. No network."""

    def __init__(self, sql="SELECT 1"):
        self.sql = sql
        self.prompts = []

    async def ainvoke(self, messages):
        prompt = messages[0].content
        self.prompts.append(prompt)

        class _Response:
            def __init__(self, content):
                self.content = content

        if "database analyst" in prompt:
            return _Response("A summary.")
        return _Response(self.sql)

    @property
    def generation_prompt(self):
        return next(p for p in self.prompts if "SchemaPilot" in p)

    @property
    def analysis_prompt(self):
        return next(p for p in self.prompts if "database analyst" in p)


class FakeDB:
    """Minimal DatabaseManager stand-in: a fixed catalog plus a log of executed SQL."""

    def __init__(self, catalog, engine="postgres", config=None, rows=None):
        self.catalog = catalog
        self._spec = get_spec(engine)
        self.active_id = "fake"
        self._config = config or {}
        self.executed = []
        self.rows = rows if rows is not None else [{"n": 1}]
        #: How many times introspection was actually performed -- the cost the warmed
        #: SchemaCache exists to avoid paying per question.
        self.introspections = 0

    @property
    def active_spec(self):
        return self._spec

    @property
    def active_config(self):
        return self._config

    def get_schema_metadata(self, schemas=None, max_tables=None):
        self.introspections += 1
        return self.catalog

    def execute_query(self, sql):
        self.executed.append(sql)
        return {"columns": list(self.rows[0].keys()) if self.rows else [], "rows": list(self.rows)}


def run_agent(monkeypatch, db, llm, query="how many orders per customer?", pinned=None,
              catalog=None):
    """Drive agent.execute end to end against the stubs and return the parsed events."""
    monkeypatch.setattr(agent_module, "get_db", lambda: db)
    monkeypatch.setattr(agent_module, "get_llm", lambda config=None: llm)
    monkeypatch.setattr(agent_module, "llm_target_label", lambda config=None: "stub-provider (stub-model)")
    agent_module.reset_sample_warnings()

    async def drive():
        events = []
        agent = SchemaPilotAgent()
        async for chunk in agent.execute(query, pinned_tables=pinned, catalog=catalog):
            if chunk.strip():
                events.append(json.loads(chunk))
        return events

    return asyncio.run(drive())


# --------------------------------------------------------------------------- prompt assembly


def test_prompt_has_the_right_dialect_no_ddl_no_recharts_no_samples(monkeypatch, wide_catalog):
    db = FakeDB(wide_catalog, engine="postgres")
    llm = StubLLM()
    events = run_agent(monkeypatch, db, llm)

    prompt = llm.generation_prompt
    assert "postgres SQL" in prompt
    assert "CREATE TABLE" not in prompt
    assert "Recharts" not in prompt
    assert "read-only" in prompt.lower()

    # Samples are off by default, so the only SQL executed is the generated query itself.
    assert db.executed == ["SELECT 1"]
    assert "sample rows" not in prompt

    for event in events:
        assert "Recharts" not in json.dumps(event)
    assert {e["event"] for e in events} >= {"agent_message", "schema_selection", "swarm_completed"}


def test_dialect_comes_from_the_spec_not_the_sqlalchemy_dialect_name(monkeypatch, wide_catalog):
    """clickhouse-connect calls itself 'clickhousedb'; the prompt must say 'clickhouse'."""
    db = FakeDB(wide_catalog, engine="clickhouse")
    llm = StubLLM()
    run_agent(monkeypatch, db, llm)

    prompt = llm.generation_prompt
    assert "clickhouse SQL" in prompt
    assert "clickhousedb" not in prompt
    # prompt_notes are appended so the model does not invent FK joins ClickHouse cannot report.
    assert "no foreign-key metadata" in prompt


def test_three_part_naming_instruction_only_for_three_part_engines(monkeypatch, wide_catalog):
    trino_llm = StubLLM()
    run_agent(monkeypatch, FakeDB(wide_catalog, engine="trino"), trino_llm)
    assert "THREE parts" in trino_llm.generation_prompt

    pg_llm = StubLLM()
    run_agent(monkeypatch, FakeDB(wide_catalog, engine="postgres"), pg_llm)
    assert "THREE parts" not in pg_llm.generation_prompt


def test_schema_selection_event_carries_tables_and_scores(monkeypatch, wide_catalog):
    llm = StubLLM()
    events = run_agent(monkeypatch, FakeDB(wide_catalog), llm,
                       pinned=["public.zz_audit_log_03"])

    selection = next(e for e in events if e["event"] == "schema_selection")
    assert "public.orders" in selection["tables"]
    assert "public.zz_audit_log_03" in selection["tables"]
    assert selection["scores"]["public.orders"] > 0
    assert len(selection["tables"]) <= settings.MAX_PROMPT_TABLES


def test_only_selected_tables_reach_the_prompt(monkeypatch, wide_catalog):
    llm = StubLLM()
    events = run_agent(monkeypatch, FakeDB(wide_catalog), llm)
    selected = next(e for e in events if e["event"] == "schema_selection")["tables"]

    prompt = llm.generation_prompt
    for table in selected:
        assert f"TABLE {table}" in prompt
    for table in wide_catalog["tables"]:
        if table not in selected:
            assert f"TABLE {table}" not in prompt


# --------------------------------------------------------------------------- catalog ownership


def test_supplied_catalog_means_the_agent_does_not_introspect(monkeypatch, wide_catalog):
    """A question after a warm must not re-run introspection.

    This is the single-owner contract in practice: the REPL warms ``SchemaCache`` on ``/use`` and
    hands the result over, so completion, ``/schema``, pin resolution and the LLM prompt all see
    the same catalog and a slow warehouse is introspected once per connection, not once per
    question.
    """
    db = FakeDB(wide_catalog)
    llm = StubLLM()
    events = run_agent(monkeypatch, db, llm, catalog=wide_catalog)

    assert db.introspections == 0
    # ...and the supplied catalog is genuinely what was used.
    selected = next(e for e in events if e["event"] == "schema_selection")["tables"]
    assert "public.orders" in selected


def test_agent_still_introspects_when_no_catalog_is_supplied(monkeypatch, wide_catalog):
    """The one-shot ``schemapilot "question"`` path has no session, so it must self-serve."""
    db = FakeDB(wide_catalog)
    run_agent(monkeypatch, db, StubLLM())
    assert db.introspections == 1


def test_supplied_catalog_wins_over_the_databases_own(monkeypatch, wide_catalog, small_catalog):
    """Proves the injected catalog is used rather than merely accepted and ignored."""
    db = FakeDB(wide_catalog)
    llm = StubLLM()
    events = run_agent(monkeypatch, db, llm, query="list the widgets", catalog=small_catalog)

    selected = next(e for e in events if e["event"] == "schema_selection")["tables"]
    assert sorted(selected) == sorted(small_catalog["tables"])
    assert db.introspections == 0


def test_supplied_empty_catalog_reports_no_tables(monkeypatch, wide_catalog):
    """An empty warmed catalog must not silently fall back to introspecting."""
    db = FakeDB(wide_catalog)
    events = run_agent(monkeypatch, db, StubLLM(),
                       catalog={"tables": {}, "relationships": [], "truncated": False,
                                "total_tables": 0})

    assert db.introspections == 0
    assert any(e["event"] == "final_output" and "No tables detected" in e["text"] for e in events)


# --------------------------------------------------------------------------- sampling


def test_samples_only_when_opted_in_and_only_for_shortlisted_tables(monkeypatch, wide_catalog):
    db = FakeDB(wide_catalog, engine="postgres", config={"include_samples": True},
                rows=[{"id": 1, "name": "ada"}, {"id": 2, "name": "bob"},
                      {"id": 3, "name": "cy"}, {"id": 4, "name": "dee"}])
    llm = StubLLM()
    events = run_agent(monkeypatch, db, llm)
    selected = next(e for e in events if e["event"] == "schema_selection")["tables"]

    sample_sql = [s for s in db.executed if s.startswith("SELECT * FROM")]
    assert sample_sql, db.executed
    assert len(sample_sql) == len(selected)

    expected = {build_sample_query(get_spec("postgres"), t, wide_catalog["tables"][t])
                for t in selected}
    assert set(sample_sql) == expected
    for statement in sample_sql:
        assert statement.endswith(f"LIMIT {SAMPLE_ROW_LIMIT}")
        assert '"' in statement  # quoted, not raw-interpolated

    # The rows do reach the prompt, but never more than SAMPLE_ROW_LIMIT of them per table.
    prompt = llm.generation_prompt
    assert "sample rows" in prompt
    assert '"dee"' not in prompt and "dee" not in prompt

    warning = next(e for e in events if e["event"] == "agent_message"
                   and "include_samples" in e.get("message", ""))
    assert "stub-provider" in warning["message"]


def test_sampling_uses_metadata_components_not_the_dotted_key(monkeypatch):
    """End to end: a SQLite table whose NAME contains a dot is sampled as one identifier."""
    tables = {
        "sales.2024": _table("sales.2024", ["id", "amount"], schema=None, table="sales.2024"),
        "customers": _table("customers", ["id", "name"], schema=None, table="customers"),
    }
    catalog = {"tables": tables, "relationships": [], "truncated": False, "total_tables": 2}
    db = FakeDB(catalog, engine="sqlite", config={"include_samples": True},
                rows=[{"id": 1, "amount": 5}])

    run_agent(monkeypatch, db, StubLLM(), query="total sales in 2024", catalog=catalog)

    assert 'SELECT * FROM "sales.2024" LIMIT 3' in db.executed
    # The wrong reading -- two identifiers -- must never be emitted.
    assert 'SELECT * FROM "sales"."2024" LIMIT 3' not in db.executed


def test_sample_warning_is_emitted_once_per_connection(monkeypatch, wide_catalog):
    db = FakeDB(wide_catalog, config={"include_samples": True})
    monkeypatch.setattr(agent_module, "get_db", lambda: db)
    monkeypatch.setattr(agent_module, "get_llm", lambda config=None: StubLLM())
    agent_module.reset_sample_warnings()

    async def drive():
        seen = []
        for _ in range(2):
            count = 0
            async for chunk in SchemaPilotAgent().execute("how many orders?"):
                event = json.loads(chunk)
                if event["event"] == "agent_message" and "include_samples" in event.get("message", ""):
                    count += 1
            seen.append(count)
        return seen

    assert asyncio.run(drive()) == [1, 0]


# --------------------------------------------------------------------------- analyst


def _dataset_payload(prompt):
    """Pull the JSON dataset back out of the analyst prompt."""
    body = prompt.split("Dataset (JSON): ", 1)[1]
    return json.loads(body.split("\n", 1)[0].strip())


def test_analyst_input_is_capped_at_analyst_sample_rows(monkeypatch, small_catalog):
    rows = [{"id": i} for i in range(500)]
    db = FakeDB(small_catalog, rows=rows)
    llm = StubLLM()
    run_agent(monkeypatch, db, llm)

    prompt = llm.analysis_prompt
    assert "Recharts" not in prompt
    assert "Total rows returned: 500" in prompt
    payload = _dataset_payload(prompt)
    assert len(payload["rows"]) == settings.ANALYST_SAMPLE_ROWS == 30


def test_analyst_cap_is_configurable(monkeypatch, small_catalog):
    monkeypatch.setattr(settings, "ANALYST_SAMPLE_ROWS", 5)
    db = FakeDB(small_catalog, rows=[{"id": i} for i in range(50)])
    llm = StubLLM()
    run_agent(monkeypatch, db, llm)

    payload = _dataset_payload(llm.analysis_prompt)
    assert len(payload["rows"]) == 5


def test_swarm_completed_stays_backward_compatible(monkeypatch, small_catalog):
    events = run_agent(monkeypatch, FakeDB(small_catalog), StubLLM())
    completed = next(e for e in events if e["event"] == "swarm_completed")

    assert set(completed) == {"event", "sql", "summary", "raw_data"}
    # cli.py reads sql / summary / raw_data{columns,rows}; a half-populated chart is worse than
    # none, so the key is gone entirely.
    assert "chart" not in completed
    assert completed["raw_data"]["rows"]
