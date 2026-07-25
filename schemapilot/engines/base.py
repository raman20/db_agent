"""Declarative engine specifications.

An engine is *data*, not a subclass hierarchy: everything SchemaPilot needs to know about a
backend lives in a frozen :class:`EngineSpec`. Adding a new SQL engine means adding one small
module under ``schemapilot/engines/`` and registering it -- no interface to implement.

The three callables (``build_uri``, ``connect_args``, ``engine_kwargs``) exist because those
genuinely differ per driver; everything else is a field.
"""

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, FrozenSet, Tuple

from sqlalchemy.engine import URL


def _default_build_uri(spec: "EngineSpec", config: Dict[str, Any]) -> URL:
    """Standard host/port/user/password/database URL.

    Always built with :meth:`sqlalchemy.engine.URL.create`, never string interpolation:
    a password such as ``p@ss/w:rd`` silently corrupts an interpolated URI (the password
    parses as ``p`` and the host becomes ``ss``), producing a connection to the wrong server.
    """
    return URL.create(
        drivername=spec.sqlalchemy_url_driver,
        username=config.get("username") or None,
        password=config.get("password") or None,
        host=config.get("host") or "localhost",
        port=int(config.get("port") or spec.default_port) if (config.get("port") or spec.default_port) else None,
        database=config.get("database") or None,
    )


def _default_connect_args(spec: "EngineSpec", config: Dict[str, Any]) -> Dict[str, Any]:
    return {"connect_timeout": 5}


def _default_engine_kwargs(spec: "EngineSpec", config: Dict[str, Any]) -> Dict[str, Any]:
    return {"pool_size": 5, "max_overflow": 5, "pool_recycle": 1800}


@dataclass(frozen=True)
class EngineSpec:
    """Everything SchemaPilot needs to know about one SQL engine."""

    name: str
    display_name: str
    sqlalchemy_url_driver: str
    sqlglot_dialect: str

    default_port: int = 0
    pip_extra: str = ""

    #: Depth of SQL name qualification: 1 = ``table``, 2 = ``schema.table``,
    #: 3 = ``catalog.schema.table``. Drives prompt instructions and introspection keys.
    sql_name_parts: int = 1

    #: Which fields ``--add-conn`` should prompt for. Deliberately SEPARATE from
    #: ``sql_name_parts``: DuckDB has 3-part SQL names but its connection input is a file path.
    #: Inferring one from the other is what broke DuckDB in the earlier design.
    connection_fields: Tuple[str, ...] = ("host", "port", "username", "password", "database")

    quote_char: str = '"'
    supports_transactions: bool = True

    #: Whether SchemaPilot can *guarantee* a rollback, and therefore offer an honest dry-run.
    #: Trino's DBAPI does expose commit/rollback, but real support is per-connector -- so this
    #: is a statement about SchemaPilot's guarantee, not about the engine's platform.
    supports_safe_dry_run: bool = True

    has_information_schema: bool = True
    row_limit_clause: str = "LIMIT {n}"

    #: Leading keywords of unparsed statements (``exp.Command``) that are read-only on this
    #: engine. NOTE: ``EXPLAIN`` is deliberately absent everywhere -- see security.py.
    readonly_commands: FrozenSet[str] = frozenset()

    #: sqlglot expression class names that are read-only on this engine (``Show``, ``Describe``).
    #: ``Pragma`` is deliberately NOT allowed here: a class-wide allowance let writable pragmas
    #: such as ``PRAGMA user_version = 4242`` and ``PRAGMA writable_schema = ON`` through. Pragmas
    #: are gated by name via ``readonly_pragmas`` instead.
    readonly_nodes: FrozenSet[str] = frozenset({"Show", "Describe"})

    #: Names of pragmas that only *read* state on this engine, ALWAYS lowercase. Only the bare
    #: ``PRAGMA <name>`` form is ever allowed -- see ``security.py`` for why the argument form
    #: (``PRAGMA table_info(t)``) cannot be told apart from an assignment.
    readonly_pragmas: FrozenSet[str] = frozenset()

    #: Function names to block. ALWAYS stored lowercase -- comparison is against
    #: ``node.name.lower()``, so a camelCase entry would never match.
    dangerous_functions: FrozenSet[str] = frozenset()

    #: sqlglot expression class names to block. Needed because some table functions get their
    #: own node type rather than ``exp.Anonymous`` (``read_csv`` -> ``exp.ReadCSV``), so a
    #: name-only check silently misses them.
    dangerous_nodes: FrozenSet[str] = frozenset()

    system_schemas: FrozenSet[str] = frozenset()

    #: ``"plain"`` -> ``get_schema_names()`` returns bare schema names.
    #: ``"composite"`` -> it returns ``db.schema`` pairs (DuckDB), parsed on the last dot.
    schema_name_style: str = "plain"

    #: Which connection-profile key holds the namespace introspection should reflect, or None
    #: when the engine has no schema level to scope to. Declared per engine because the key
    #: DIFFERS from the SQL concept: MySQL and ClickHouse call their schema level a "database"
    #: and collect it as ``database``, while PostgreSQL and Trino collect ``schema`` alongside a
    #: separate database/catalog. Assuming ``schema`` everywhere made introspection enumerate
    #: every accessible MySQL/ClickHouse database instead of the configured one.
    introspection_namespace_field: str = None

    #: Human-readable note about cross-engine reach, or None. Deliberately not called
    #: "federation", which would overclaim.
    cross_engine_via: str = None

    #: Extra guidance appended to the SQL-generation prompt for this engine.
    prompt_notes: str = ""

    _build_uri: Callable[..., Any] = field(default=_default_build_uri, repr=False)
    _connect_args: Callable[..., Any] = field(default=_default_connect_args, repr=False)
    _engine_kwargs: Callable[..., Any] = field(default=_default_engine_kwargs, repr=False)

    def build_uri(self, config: Dict[str, Any]) -> URL:
        return self._build_uri(self, config)

    def connect_args(self, config: Dict[str, Any]) -> Dict[str, Any]:
        return self._connect_args(self, config)

    def engine_kwargs(self, config: Dict[str, Any]) -> Dict[str, Any]:
        return self._engine_kwargs(self, config)

    def install_hint(self) -> str:
        """The exact pip command that makes this engine work, or '' if it needs no extra."""
        if not self.pip_extra:
            return ""
        return f"pip install 'schemapilot[{self.pip_extra}]'"
