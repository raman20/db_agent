"""The security sentry: an AST-level allowlist for SQL SchemaPilot is about to execute.

Design in one line: **classify the top-level statement, then walk for nested violations.**

Why an allowlist and not a denylist of "mutating" node types (what this module used to be)?
Verified against sqlglot 30.13.0: ``TRUNCATE TABLE t`` parses to ``exp.TruncateTable``, which was
absent from the old ``forbidden_mutation_types`` tuple and therefore sailed straight through --
as did ``exp.Merge``, ``exp.Grant``, ``exp.Set``, ``exp.Use``, ``exp.Attach``, ``exp.Detach``,
``exp.Export``, ``exp.Refresh`` and ``exp.Cache``. A denylist is only ever as complete as the
last time someone read the sqlglot changelog; an allowlist fails closed when sqlglot grows a new
node type.

Why classify the top level *first* instead of only walking? Because ``exp.Command`` (sqlglot's
"I could not parse this" node) can only be judged from its leading keyword, which lives in
``Command.this`` as a plain string. Walk-only classification cannot tell read-only ``SHOW`` from
state-mutating ``SYSTEM``/``OPTIMIZE`` -- they are the *same node type* -- which is exactly why
the old code had to blanket-block ``exp.Command`` and thereby rejected ``SHOW TABLES``.

**Stated limit: this is a DDL/DML guard, not a sandbox.** Read-shaped SQL can still reach the
filesystem or network through engine table functions; ``dangerous_functions`` raises the bar,
but least-privilege database credentials are the real control.
"""

import logging
from typing import Optional, Tuple

import sqlglot
from sqlglot import exp

from schemapilot.engines.base import EngineSpec
from schemapilot.engines.registry import get_spec


class _CommandFallbackFilter(logging.Filter):
    """Drop sqlglot's "falling back to parsing as a 'Command'" warning.

    Statements sqlglot cannot fully parse (``SHOW CATALOGS``, ``OPTIMIZE TABLE t FINAL``, ...)
    are *expected* input here and are classified explicitly below, so the warning carries no
    information -- but it is emitted straight to stderr and would tear through Rich's live
    terminal output mid-render.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        return "Falling back to parsing as a 'Command'" not in record.getMessage()


logging.getLogger("sqlglot").addFilter(_CommandFallbackFilter())


#: Only these three are unlocked by ``allow_mutation=True``. Nothing else is -- see
#: ALWAYS_BLOCKED. "rw" means INSERT/UPDATE/DELETE, not "anything that changes the server".
PERMITTED_MUTATIONS = (exp.Insert, exp.Update, exp.Delete)

#: Blocked in every mode, including ``allow_mutation=True``: schema destruction, privilege
#: changes, session/state changes and data movement are never part of a generated answer.
ALWAYS_BLOCKED = (
    exp.Drop,
    exp.Alter,
    exp.Create,
    exp.TruncateTable,
    exp.Merge,
    exp.Grant,
    exp.Attach,
    exp.Detach,
    exp.Export,
    exp.Copy,
    exp.Set,
    exp.Use,
    exp.Refresh,
    exp.Cache,
    exp.Uncache,
    exp.Analyze,
)

#: Read-only statement shapes allowed on every engine. ``Intersect``, ``Except`` and ``Values``
#: are siblings of ``Union``, not subclasses in a way isinstance would cover usefully, and are
#: verified real top-level nodes for legitimate queries (``SELECT 1 INTERSECT SELECT 2``,
#: ``VALUES (1),(2)``) -- a set of only {Select, Union} default-denies valid read-only SQL.
#: ``Alias``/``Subquery`` cover ``TABLE t`` and ``(SELECT 1)``, which parse to those nodes.
READONLY_NODES = (
    exp.Select,
    exp.Union,
    exp.Intersect,
    exp.Except,
    exp.Values,
    exp.Alias,
    exp.Subquery,
)

#: Nodes whose *statement* meaning must be re-checked wherever they appear, not just at the top
#: level -- e.g. an INSERT hidden behind a CTE, or a DROP as the second half of a Block.
_STATEMENT_NODES = (exp.Command,) + ALWAYS_BLOCKED + PERMITTED_MUTATIONS

#: The pre-registry dialect mapping, kept verbatim so ``validate_sql_query`` behaves exactly as
#: before when no ``engine`` is supplied. This is the ONLY per-engine literal left in this
#: module; everything else lives in ``schemapilot/engines/``.
_LEGACY_DIALECT_ENGINES = {
    "postgres": "postgres",
    "postgresql": "postgres",
    "sqlite": "sqlite",
}
_LEGACY_DEFAULT_ENGINE = "mysql"


def _resolve_spec(engine: Optional[str], dialect: str) -> EngineSpec:
    """Pick the policy source: the named engine's spec, else the legacy dialect mapping."""
    for candidate in (engine, _LEGACY_DIALECT_ENGINES.get((dialect or "").strip().lower())):
        if not candidate:
            continue
        try:
            return get_spec(candidate)
        except ValueError:
            # An unknown engine name must not raise out of a validator whose contract is
            # (ok, message); degrade to the legacy default instead.
            continue
    return get_spec(_LEGACY_DEFAULT_ENGINE)


def _dangerous_reason(node: exp.Expression, spec: EngineSpec) -> Optional[str]:
    """Per-engine filesystem/network escape hatches, checked by node type AND by name.

    Both checks are needed: verified that ``read_csv(...)`` parses to ``exp.ReadCSV`` and
    ``read_parquet(...)`` to ``exp.ReadParquet``, so a name-only check misses them, while
    ``read_csv_auto``, ``read_json``, ``glob``, ``s3`` and ``url`` parse to ``exp.Anonymous``,
    where only the name identifies them.
    """
    node_name = type(node).__name__
    if node_name in spec.dangerous_nodes:
        return (
            f"Security Violation: '{node_name}' is blocked on {spec.display_name} "
            "because it can reach the filesystem or network."
        )
    if isinstance(node, exp.Func):
        # Only unrecognised functions (Anonymous / AnonymousAggFunc) carry a name here; typed
        # nodes such as exp.Sum report name="" and are never in the (lowercase) blocklist.
        func_name = (node.name or "").lower()
        if func_name and func_name in spec.dangerous_functions:
            return f"Security Violation: invocation of blocked system function '{func_name}'."
    return None


def _statement_reason(
    node: exp.Expression, spec: EngineSpec, allow_mutation: bool
) -> Optional[str]:
    """Classify one statement. Returns None when permitted, else the violation message."""
    if isinstance(node, exp.Block):
        # `SELECT 1; DROP TABLE users` parses to a SINGLE exp.Block holding both statements, so
        # a Block is safe only if EVERY child is: classify them all and reject the whole input
        # on the first failure. walk() does happen to reach the nested Drop, but leaning on that
        # is what forced the old blanket ban on exp.Command -- top-level classification has to
        # recurse here or multi-statement injection regresses.
        for child in node.expressions:
            reason = _statement_reason(child, spec, allow_mutation)
            if reason:
                return reason
        return None

    if isinstance(node, exp.Command):
        # sqlglot stores the leading keyword of an unparsed statement in Command.this as a plain
        # string (SHOW / EXPLAIN / SYSTEM / OPTIMIZE), which is the only handle we get on it.
        keyword = str(node.this).strip().upper()
        if keyword in spec.readonly_commands:
            return None
        # Blocked even with allow_mutation=True: we cannot reason about the effects of a
        # statement sqlglot could not parse. Note EXPLAIN is deliberately in no engine's
        # readonly_commands -- `EXPLAIN ANALYZE DELETE FROM users` also parses to
        # Command(this='EXPLAIN') and its walk() sees only ['Command', 'Literal'], so the DELETE
        # is invisible, while Postgres EXPLAIN ANALYZE genuinely executes it.
        return (
            f"Security Violation: statement '{keyword}' is not an allowed read-only command "
            f"on {spec.display_name}."
        )

    dangerous = _dangerous_reason(node, spec)
    if dangerous:
        return dangerous

    if isinstance(node, ALWAYS_BLOCKED):
        action = type(node).__name__.upper()
        return (
            f"Security Violation: operation '{action}' is blocked in every mode, including "
            "read-write sessions."
        )

    if isinstance(node, PERMITTED_MUTATIONS):
        if allow_mutation:
            return None
        action = type(node).__name__.upper()
        return f"Security Violation: mutating operation '{action}' is disabled in the current session."

    # Read-only: the shared shapes, plus whatever this engine declares (Show / Pragma /
    # Describe are stored as class-name strings on the spec).
    if isinstance(node, READONLY_NODES) or type(node).__name__ in spec.readonly_nodes:
        return None

    # Default deny. A node type nobody has classified is a node type nobody has reasoned about,
    # so future sqlglot additions fail closed rather than open.
    return (
        f"Security Violation: statement type '{type(node).__name__}' is not on the "
        f"{spec.display_name} allowlist and is blocked by default."
    )


def validate_sql_query(
    sql: str,
    dialect: str = "mysql",
    allow_mutation: bool = False,
    engine: Optional[str] = None,
) -> Tuple[bool, str]:
    """Validate SQL against the per-engine allowlist.

    Args:
        sql: the statement to check.
        dialect: legacy parameter, honoured when ``engine`` is not given.
        allow_mutation: unlocks INSERT/UPDATE/DELETE and nothing else.
        engine: an engine registry key (``postgres``, ``trino``, ``duckdb``, ...). When given,
            the whole policy -- parse dialect, read-only commands, dangerous functions -- comes
            from that engine's spec.

    Returns:
        ``(is_safe, message)``. The message is user-facing and explains the refusal.
    """
    cleaned_sql = (sql or "").strip().strip(";").strip()
    if not cleaned_sql:
        return False, "Empty query string provided."

    spec = _resolve_spec(engine, dialect)

    try:
        expression = sqlglot.parse_one(cleaned_sql, read=spec.sqlglot_dialect)
    except sqlglot.errors.ParseError as e:
        return False, f"SQL Syntax Error: Failed to parse query. Details: {str(e)}"
    except Exception as e:
        return False, f"Unexpected error during query parsing: {str(e)}"

    reason = _statement_reason(expression, spec, allow_mutation)
    if reason:
        return False, reason

    # The top level is legitimate; now hunt for violations buried inside it -- a mutation behind
    # a CTE, a dangerous table function in a FROM clause, a DDL child of a Block.
    for node in expression.walk():
        dangerous = _dangerous_reason(node, spec)
        if dangerous:
            return False, dangerous
        if node is expression:
            continue
        if isinstance(node, _STATEMENT_NODES):
            reason = _statement_reason(node, spec, allow_mutation)
            if reason:
                return False, reason

    return True, "Query is safe to execute."
