import pytest
import sqlglot
from sqlglot import exp

from schemapilot.security import validate_sql_query

def test_safe_queries():
    # Standard SELECT queries should be verified as safe
    safe, msg = validate_sql_query("SELECT id, name FROM users LIMIT 10")
    assert safe
    assert "safe" in msg.lower()
    
    # Complicated Joins should be safe
    safe, msg = validate_sql_query(
        "SELECT u.name, o.total FROM users u JOIN orders o ON u.id = o.user_id WHERE o.total > 100"
    )
    assert safe

def test_mutations_blocked():
    # DROP queries must be blocked
    safe, msg = validate_sql_query("DROP TABLE users")
    assert not safe
    assert "violation" in msg.lower() or "disabled" in msg.lower()
    
    # INSERT queries must be blocked when allow_mutation is False
    safe, msg = validate_sql_query("INSERT INTO users (name) VALUES ('Test')", allow_mutation=False)
    assert not safe
    
    # UPDATE queries must be blocked when allow_mutation is False
    safe, msg = validate_sql_query("UPDATE users SET name = 'Test' WHERE id = 1", allow_mutation=False)
    assert not safe

def test_mutations_allowed_when_configured():
    # INSERT queries must pass when allow_mutation is True
    safe, msg = validate_sql_query("INSERT INTO users (name) VALUES ('Test')", allow_mutation=True)
    assert safe

def test_malicious_functions_blocked():
    # Block load_file function
    safe, msg = validate_sql_query("SELECT load_file('/etc/passwd')")
    assert not safe
    assert "blocked" in msg.lower() or "violation" in msg.lower()
    
    # Block COPY statement
    safe, msg = validate_sql_query("COPY users TO '/tmp/users.txt'")
    assert not safe


# ---------------------------------------------------------------------------
# T4: allowlist sentry. Everything below asserts a *verified* sqlglot 30.13.0
# behaviour, so a sqlglot upgrade that changes an AST shape fails loudly here.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "TRUNCATE TABLE users",
        "MERGE INTO a USING b ON a.id = b.id WHEN MATCHED THEN DELETE",
        "GRANT SELECT ON users TO bob",
        "SET autocommit = 1",
        "USE other_db",
    ],
)
def test_state_changing_statements_blocked_even_in_rw_mode(sql):
    """`rw` unlocks INSERT/UPDATE/DELETE and nothing else."""
    safe, msg = validate_sql_query(sql, allow_mutation=True)
    assert not safe, sql
    assert "blocked in every mode" in msg


def test_attach_blocked_even_in_rw_mode():
    # ATTACH is only parseable on engines that have it; sqlite/duckdb both do.
    safe, msg = validate_sql_query("ATTACH DATABASE '/tmp/x.db' AS x", engine="sqlite", allow_mutation=True)
    assert not safe
    assert "blocked" in msg.lower()


def test_truncate_regression_node_type():
    """The headline bug: TRUNCATE is exp.TruncateTable, which the old denylist never listed."""
    assert isinstance(sqlglot.parse_one("TRUNCATE TABLE t", read="mysql"), exp.TruncateTable)


@pytest.mark.parametrize("sql", ["DROP TABLE users", "ALTER TABLE users ADD c INT", "CREATE TABLE t (id INT)"])
def test_ddl_still_blocked_in_rw_mode(sql):
    safe, msg = validate_sql_query(sql, allow_mutation=True)
    assert not safe, sql
    assert "blocked in every mode" in msg


def test_multi_statement_block_rejected():
    """`SELECT 1; DROP TABLE users` is a single exp.Block; every child must be classified."""
    parsed = sqlglot.parse_one("SELECT 1; DROP TABLE users", read="mysql")
    assert isinstance(parsed, exp.Block)
    assert [type(e).__name__ for e in parsed.expressions] == ["Select", "Drop"]

    safe, msg = validate_sql_query("SELECT 1; DROP TABLE users")
    assert not safe
    assert "DROP" in msg

    # A Block of only read-only statements is fine.
    safe, _ = validate_sql_query("SELECT 1; SELECT 2")
    assert safe


def test_block_child_mutation_respects_mode():
    safe, _ = validate_sql_query("SELECT 1; DELETE FROM users WHERE id = 1")
    assert not safe
    safe, _ = validate_sql_query("SELECT 1; DELETE FROM users WHERE id = 1", allow_mutation=True)
    assert safe


# --------------------------------------------------------------------------- exp.Command


def test_show_allowlisted_per_engine():
    """SHOW is an unparsed exp.Command; only engines that declare it may run it."""
    assert isinstance(sqlglot.parse_one("SHOW CATALOGS", read="trino"), exp.Command)

    safe, _ = validate_sql_query("SHOW CATALOGS", engine="trino")
    assert safe
    safe, _ = validate_sql_query("SHOW TABLES", engine="trino")
    assert safe
    safe, _ = validate_sql_query("SHOW STATS FOR orders", engine="trino")
    assert safe

    # Postgres has no SHOW in its readonly_commands, so the same statement is refused.
    safe, msg = validate_sql_query("SHOW CATALOGS", engine="postgres")
    assert not safe
    assert "SHOW" in msg


def test_clickhouse_state_changing_commands_blocked():
    """SYSTEM and OPTIMIZE are the SAME node type as SHOW -- hence an allowlist, not a denylist."""
    for sql in ("SYSTEM FLUSH LOGS", "OPTIMIZE TABLE t FINAL"):
        assert isinstance(sqlglot.parse_one(sql, read="clickhouse"), exp.Command)
        safe, msg = validate_sql_query(sql, engine="clickhouse")
        assert not safe, sql
        assert "read-only command" in msg
        # Not unlockable: we cannot reason about SQL sqlglot could not parse.
        safe, _ = validate_sql_query(sql, engine="clickhouse", allow_mutation=True)
        assert not safe, sql


def test_unknown_command_blocked_even_in_rw_mode():
    safe, _ = validate_sql_query("KILL QUERY 1", engine="clickhouse", allow_mutation=True)
    assert not safe


# --------------------------------------------------------------------------- EXPLAIN


def test_explain_analyze_hides_its_statement():
    """Regression guard for why EXPLAIN is allowlisted nowhere.

    Postgres EXPLAIN ANALYZE genuinely executes its statement, but sqlglot parses the whole thing
    to Command(this='EXPLAIN') whose walk() never yields the DELETE.
    """
    parsed = sqlglot.parse_one("EXPLAIN ANALYZE DELETE FROM users", read="postgres")
    assert isinstance(parsed, exp.Command)
    assert str(parsed.this).upper() == "EXPLAIN"
    assert sorted({type(n).__name__ for n in parsed.walk()}) == ["Command", "Literal"]


@pytest.mark.parametrize(
    "sql",
    [
        "EXPLAIN ANALYZE DELETE FROM users",
        "EXPLAIN (ANALYZE true) UPDATE users SET name = 'x'",
        "EXPLAIN SELECT 1",  # blocked in v1 on purpose: EXPLAIN is in no readonly_commands
    ],
)
def test_explain_blocked_everywhere(sql):
    safe, msg = validate_sql_query(sql, engine="postgres")
    assert not safe, sql
    assert "EXPLAIN" in msg
    safe, _ = validate_sql_query(sql, engine="postgres", allow_mutation=True)
    assert not safe, sql


# --------------------------------------------------------------------------- read-only shapes


@pytest.mark.parametrize(
    "sql, expected_node",
    [
        ("SELECT 1 INTERSECT SELECT 2", exp.Intersect),
        ("SELECT 1 EXCEPT SELECT 2", exp.Except),
        ("SELECT 1 UNION SELECT 2", exp.Union),
        ("VALUES (1), (2)", exp.Values),
        ("WITH a AS (SELECT 1 AS x) SELECT x FROM a", exp.Select),
        ("TABLE t", exp.Alias),
    ],
)
def test_legitimate_readonly_top_level_nodes_allowed(sql, expected_node):
    """A read-only set of only {Select, Union} would default-deny all of these."""
    assert isinstance(sqlglot.parse_one(sql, read="postgres"), expected_node)
    safe, msg = validate_sql_query(sql, engine="postgres")
    assert safe, f"{sql}: {msg}"


def test_pragma_per_engine():
    assert isinstance(sqlglot.parse_one("PRAGMA database_list", read="duckdb"), exp.Pragma)
    safe, _ = validate_sql_query("PRAGMA database_list", engine="duckdb")
    assert safe
    # MySQL has no PRAGMA at all, so its readonly_pragmas set is empty.
    safe, msg = validate_sql_query("PRAGMA database_list", engine="mysql")
    assert not safe
    assert "read-only pragma" in msg


def test_duckdb_and_trino_readonly_metadata_statements():
    assert isinstance(sqlglot.parse_one("SHOW TABLES", read="duckdb"), exp.Show)
    safe, _ = validate_sql_query("SHOW TABLES", engine="duckdb")
    assert safe

    assert isinstance(sqlglot.parse_one("DESCRIBE t", read="trino"), exp.Describe)
    safe, _ = validate_sql_query("DESCRIBE t", engine="trino")
    assert safe

    assert isinstance(sqlglot.parse_one("DESCRIBE TABLE t", read="clickhouse"), exp.Describe)
    safe, _ = validate_sql_query("DESCRIBE TABLE t", engine="clickhouse")
    assert safe


def test_engine_specific_dialects_are_actually_used():
    """Parsing Trino/ClickHouse SQL as MySQL is what produced bogus syntax errors before."""
    safe, msg = validate_sql_query("SELECT 1 SETTINGS max_threads = 4", engine="clickhouse")
    assert safe, msg
    safe, msg = validate_sql_query("SELECT uniqExact(id) FROM events", engine="clickhouse")
    assert safe, msg
    safe, msg = validate_sql_query("SELECT approx_distinct(id) FROM events", engine="trino")
    assert safe, msg


def test_clickhouse_truncate_blocked():
    assert isinstance(sqlglot.parse_one("TRUNCATE TABLE t", read="clickhouse"), exp.TruncateTable)
    safe, _ = validate_sql_query("TRUNCATE TABLE t", engine="clickhouse", allow_mutation=True)
    assert not safe


# --------------------------------------------------------------------------- dangerous functions


def test_duckdb_file_readers_blocked_by_node_type():
    """read_csv/read_parquet get their own node types, so a name-only check misses them."""
    assert isinstance(sqlglot.parse_one("read_csv('/etc/passwd')", read="duckdb"), exp.ReadCSV)
    assert isinstance(sqlglot.parse_one("read_parquet('x')", read="duckdb"), exp.ReadParquet)

    for sql in (
        "SELECT * FROM read_csv('/etc/passwd')",
        "SELECT * FROM read_parquet('/etc/shadow')",
        "read_csv('/etc/passwd')",
    ):
        safe, msg = validate_sql_query(sql, engine="duckdb")
        assert not safe, sql
        assert "blocked" in msg.lower()


def test_duckdb_anonymous_file_readers_blocked_by_name():
    for sql in (
        "SELECT * FROM read_csv_auto('/etc/passwd')",
        "SELECT * FROM read_json('/etc/passwd')",
        "SELECT * FROM glob('/etc/*')",
    ):
        safe, msg = validate_sql_query(sql, engine="duckdb")
        assert not safe, sql
        assert "blocked" in msg.lower()


def test_clickhouse_network_functions_blocked():
    for sql in ("SELECT * FROM s3('http://evil/x', 'CSV')", "SELECT * FROM url('http://evil/x', 'CSV')"):
        safe, msg = validate_sql_query(sql, engine="clickhouse")
        assert not safe, sql
        assert "blocked system function" in msg


def test_typed_functions_are_not_mistaken_for_blocked_names():
    """exp.Sum etc. report name="" -- a naive .name check would be a false positive risk."""
    safe, msg = validate_sql_query("SELECT sum(bytes), max(id) FROM logs", engine="clickhouse")
    assert safe, msg


# --------------------------------------------------------------------------- nested violations


def test_mutation_hidden_in_cte_blocked():
    """Top-level classification alone would see only the Insert's wrapper; the walk catches it."""
    sql = "WITH x AS (SELECT 1) INSERT INTO t SELECT * FROM x"
    safe, msg = validate_sql_query(sql, engine="postgres")
    assert not safe
    assert "INSERT" in msg
    safe, _ = validate_sql_query(sql, engine="postgres", allow_mutation=True)
    assert safe


def test_nested_ddl_in_subquery_blocked():
    safe, _ = validate_sql_query(
        "SELECT * FROM (SELECT 1) x; DROP TABLE users", engine="postgres", allow_mutation=True
    )
    assert not safe


def test_dangerous_function_inside_permitted_mutation_blocked():
    safe, msg = validate_sql_query(
        "INSERT INTO t SELECT * FROM read_csv('/etc/passwd')", engine="duckdb", allow_mutation=True
    )
    assert not safe
    assert "blocked" in msg.lower()


# --------------------------------------------------------------------------- plumbing


def test_legacy_dialect_fallback_without_engine():
    """No `engine=` must behave exactly as before: postgres/sqlite mapped, everything else mysql."""
    safe, _ = validate_sql_query("SELECT * FROM t WHERE x ILIKE 'a'", dialect="postgres")
    assert safe
    safe, _ = validate_sql_query("SELECT 1", dialect="totally-unknown")
    assert safe
    # Legacy default is mysql, which does not allow PRAGMA.
    safe, _ = validate_sql_query("PRAGMA database_list")
    assert not safe


def test_unknown_engine_name_is_refused_not_downgraded():
    """An explicit engine we do not know must not silently receive MySQL policy."""
    for bad in ("oracle", "postgress"):
        safe, msg = validate_sql_query("SELECT 1", engine=bad)
        assert not safe, bad
        assert "Unsupported engine" in msg
        assert bad in msg
    # Registry aliases still resolve.
    safe, _ = validate_sql_query("SELECT 1", engine="postgresql")
    assert safe


def test_empty_and_unparseable_input():
    safe, msg = validate_sql_query("   ;  ")
    assert not safe
    assert "empty" in msg.lower()

    safe, msg = validate_sql_query("SELECT FROM FROM WHERE )(", engine="postgres")
    assert not safe


def test_command_fallback_warning_is_silenced(caplog):
    """sqlglot's fallback warning would tear through Rich's live output."""
    with caplog.at_level("WARNING", logger="sqlglot"):
        validate_sql_query("SHOW CATALOGS", engine="trino")
    assert not [r for r in caplog.records if "Falling back to parsing as a 'Command'" in r.getMessage()]


def test_no_engine_allows_explain_nowhere():
    from schemapilot.engines.registry import ENGINES

    for spec in ENGINES.values():
        assert "EXPLAIN" not in spec.readonly_commands, spec.name


# ---------------------------------------------------------------------------
# Review regressions: four empirically reproduced bypasses.
# ---------------------------------------------------------------------------


def test_admin_statements_sharing_the_table_t_shape_are_blocked():
    """`TABLE t` has no sqlglot node; it lands in the same Alias shape as these do.

    Allowing the `Alias` class wholesale therefore allowed destructive admin statements --
    `RESET MASTER` deletes MySQL binary logs with privileged credentials.
    """
    for sql, dialect in [
        ("RESET MASTER", "mysql"),
        ("FLUSH TABLES", "mysql"),
        ("REINDEX idx_name", "sqlite"),
        ("CLUSTER users", "postgres"),
    ]:
        parsed = sqlglot.parse_one(sql, read=dialect)
        assert isinstance(parsed, exp.Alias), sql  # the shape that made this a bypass
        assert isinstance(sqlglot.parse_one("TABLE t", read=dialect), exp.Alias)

        engine = {"mysql": "mysql", "sqlite": "sqlite", "postgres": "postgres"}[dialect]
        safe, msg = validate_sql_query(sql, engine=engine)
        assert not safe, sql
        assert "blocked by default" in msg, sql
        # Not unlockable by read-write mode either.
        safe, _ = validate_sql_query(sql, engine=engine, allow_mutation=True)
        assert not safe, sql


def test_bare_table_statement_still_allowed():
    """The narrow shape the Alias allowance existed for must keep working."""
    for engine in ("mysql", "postgres", "trino", "duckdb"):
        safe, msg = validate_sql_query("TABLE orders", engine=engine)
        assert safe, f"{engine}: {msg}"


def test_select_into_is_blocked_as_ddl():
    """Postgres runs `SELECT ... INTO t` as CREATE TABLE AS, and execute_query() commits it."""
    parsed = sqlglot.parse_one("SELECT * INTO new_table FROM users", read="postgres")
    assert isinstance(parsed, exp.Select)  # an allowed root...
    assert isinstance(parsed.args.get("into"), exp.Into)  # ...with a DDL child

    for kwargs in ({}, {"allow_mutation": True}):
        safe, msg = validate_sql_query("SELECT * INTO new_table FROM users", engine="postgres", **kwargs)
        assert not safe, kwargs
        assert "INTO" in msg


def test_select_into_blocked_when_nested():
    for sql in (
        "SELECT 1 UNION SELECT * INTO t FROM u",
        "WITH a AS (SELECT * INTO z FROM u) SELECT 1",
        "SELECT * INTO TEMPORARY tt FROM users",
    ):
        safe, msg = validate_sql_query(sql, engine="postgres", allow_mutation=True)
        assert not safe, sql
        assert "INTO" in msg


def test_locking_reads_are_blocked():
    """Another side-effect-bearing child of an allowed Select root: it takes row locks."""
    parsed = sqlglot.parse_one("SELECT * FROM t FOR UPDATE", read="postgres")
    assert isinstance(parsed, exp.Select)
    safe, msg = validate_sql_query("SELECT * FROM t FOR UPDATE", engine="postgres")
    assert not safe
    assert "LOCK" in msg


@pytest.mark.parametrize(
    "sql",
    ["PRAGMA user_version = 4242", "PRAGMA journal_mode = WAL", "PRAGMA writable_schema = ON"],
)
def test_writable_pragmas_blocked(sql):
    """A class-wide Pragma allowance let these persist state from a read-only session."""
    assert isinstance(sqlglot.parse_one(sql, read="sqlite"), exp.Pragma)
    for kwargs in ({}, {"allow_mutation": True}):
        safe, msg = validate_sql_query(sql, engine="sqlite", **kwargs)
        assert not safe, (sql, kwargs)
        assert "PRAGMA <name>" in msg


def test_pragma_assignment_blocked_on_duckdb_too():
    safe, msg = validate_sql_query("PRAGMA user_version = 4242", engine="duckdb")
    assert not safe
    assert "PRAGMA <name>" in msg
    # DuckDB parses the argument form as Anonymous rather than EQ; both fail closed.
    safe, _ = validate_sql_query("PRAGMA storage_info(t)", engine="duckdb")
    assert not safe


def test_unknown_pragma_name_fails_closed():
    safe, msg = validate_sql_query("PRAGMA some_future_pragma", engine="sqlite")
    assert not safe
    assert "read-only pragma" in msg


def test_readonly_pragmas_still_allowed():
    for engine, name in [("sqlite", "database_list"), ("duckdb", "database_list"), ("duckdb", "show_tables")]:
        safe, msg = validate_sql_query(f"PRAGMA {name}", engine=engine)
        assert safe, f"{engine} {name}: {msg}"


def test_argument_pragmas_fail_closed_on_sqlite():
    """sqlglot parses `PRAGMA table_info(t)` into the same EQ node as an assignment.

    So the read form is indistinguishable from a write and must be refused; this asserts the
    cost of that decision is understood rather than accidental.
    """
    parsed = sqlglot.parse_one("PRAGMA table_info(t)", read="sqlite")
    assert isinstance(parsed.this, exp.EQ)
    safe, _ = validate_sql_query("PRAGMA table_info(t)", engine="sqlite")
    assert not safe


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT pg_read_file('/etc/passwd')",
        "SELECT pg_write_file('/tmp/x', 'y')",
        "SELECT pg_ls_dir('/etc')",
    ],
)
def test_legacy_path_keeps_the_global_dangerous_function_floor(sql):
    """Making the policy per-engine must not make any call LESS strict than before.

    These were caught by the pre-rewrite global function denylist; with no `engine=` the call
    lands on the MySQL spec, whose own set does not list them.
    """
    safe, msg = validate_sql_query(sql)
    assert not safe, sql
    assert "blocked system function" in msg


def test_global_dangerous_function_floor_applies_to_every_engine():
    from schemapilot.engines.registry import ENGINES

    for name in ENGINES:
        safe, msg = validate_sql_query("SELECT pg_read_file('/etc/passwd')", engine=name)
        assert not safe, name
        assert "blocked system function" in msg
        safe, _ = validate_sql_query("SELECT load_file('/etc/passwd')", engine=name)
        assert not safe, name
