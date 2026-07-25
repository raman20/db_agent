"""Specs for the classic relational engines: Postgres, MySQL, SQLite."""

from typing import Any, Dict

from sqlalchemy.engine import URL

from schemapilot.engines.base import EngineSpec

_FILESYSTEM_FUNCS = frozenset(
    {"load_file", "system", "cmd_exec", "sys_exec", "sys_eval"}
)

POSTGRES = EngineSpec(
    name="postgres",
    display_name="PostgreSQL",
    sqlalchemy_url_driver="postgresql+psycopg2",
    sqlglot_dialect="postgres",
    default_port=5432,
    sql_name_parts=2,
    connection_fields=("host", "port", "username", "password", "database", "schema"),
    quote_char='"',
    readonly_commands=frozenset(),
    system_schemas=frozenset({"information_schema", "pg_catalog", "pg_toast"}),
    # Postgres collects `database` and `schema` separately; the schema is the namespace.
    introspection_namespace_field="schema",
    dangerous_functions=_FILESYSTEM_FUNCS
    | {"pg_read_file", "pg_read_binary_file", "pg_write_file", "pg_ls_dir", "lo_import", "lo_export"},
    dangerous_nodes=frozenset({"Copy"}),
)

MYSQL = EngineSpec(
    name="mysql",
    display_name="MySQL",
    sqlalchemy_url_driver="mysql+pymysql",
    sqlglot_dialect="mysql",
    default_port=3306,
    sql_name_parts=2,
    connection_fields=("host", "port", "username", "password", "database"),
    quote_char="`",
    readonly_commands=frozenset({"SHOW", "DESCRIBE", "DESC"}),
    # No "Pragma": MySQL has no PRAGMA statement, so `PRAGMA ...` here is either nonsense or an
    # attempt to smuggle SQLite/DuckDB syntax past the sentry. Overridden rather than removed
    # from the default because SQLite and DuckDB do support it.
    readonly_nodes=frozenset({"Show", "Describe"}),
    system_schemas=frozenset({"information_schema", "mysql", "performance_schema", "sys"}),
    # MySQL's schema level IS its database, and that is the key --add-conn collects.
    introspection_namespace_field="database",
    dangerous_functions=_FILESYSTEM_FUNCS,
)


def _sqlite_uri(spec: EngineSpec, config: Dict[str, Any]) -> URL:
    path = config.get("path") or config.get("database") or "schemapilot.db"
    return URL.create(drivername="sqlite", database=path)


SQLITE = EngineSpec(
    name="sqlite",
    display_name="SQLite",
    sqlalchemy_url_driver="sqlite",
    sqlglot_dialect="sqlite",
    sql_name_parts=1,
    connection_fields=("path",),
    quote_char='"',
    has_information_schema=False,
    readonly_commands=frozenset(),
    readonly_nodes=frozenset({"Show", "Pragma", "Describe"}),
    dangerous_functions=frozenset({"load_extension", "readfile", "writefile", "edit", "fsdir"}),
    dangerous_nodes=frozenset({"Attach", "Detach"}),
    _build_uri=_sqlite_uri,
    _connect_args=lambda spec, config: {},
    _engine_kwargs=lambda spec, config: {},
)
