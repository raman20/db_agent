"""Specs for the analytics engines: DuckDB, Trino, ClickHouse."""

from typing import Any, Dict

from sqlalchemy.engine import URL
from sqlalchemy.pool import NullPool, StaticPool

from schemapilot.engines.base import EngineSpec

# --------------------------------------------------------------------------- DuckDB


def _duckdb_path(config: Dict[str, Any]) -> str:
    return config.get("path") or config.get("database") or ":memory:"


def _duckdb_uri(spec: EngineSpec, config: Dict[str, Any]) -> URL:
    return URL.create(drivername="duckdb", database=_duckdb_path(config))


def _duckdb_engine_kwargs(spec: EngineSpec, config: Dict[str, Any]) -> Dict[str, Any]:
    """Pooling must be conditional.

    Verified: ``NullPool`` DESTROYS ``duckdb:///:memory:`` -- every checkout opens a fresh,
    empty database, so a ``CREATE TABLE`` followed by a ``SELECT`` raises ``CatalogException``.
    ``StaticPool`` keeps the single in-memory connection alive. File-backed DuckDB has no such
    problem and prefers ``NullPool`` so the file is not held open between statements.
    """
    if _duckdb_path(config) == ":memory:":
        return {"poolclass": StaticPool}
    return {"poolclass": NullPool}


DUCKDB = EngineSpec(
    name="duckdb",
    display_name="DuckDB",
    sqlalchemy_url_driver="duckdb",
    sqlglot_dialect="duckdb",
    pip_extra="duckdb",
    sql_name_parts=3,
    # DuckDB has 3-part SQL names but connects via a FILE PATH. This is exactly why
    # connection_fields is declared rather than derived from sql_name_parts.
    connection_fields=("path",),
    quote_char='"',
    has_information_schema=True,
    readonly_commands=frozenset({"SHOW", "DESCRIBE", "DESC"}),
    system_schemas=frozenset({"system", "temp", "information_schema", "pg_catalog"}),
    schema_name_style="composite",
    dangerous_functions=frozenset(
        {"read_csv_auto", "read_json", "read_json_auto", "read_text", "read_blob", "glob", "sniff_csv"}
    ),
    # Verified: read_csv() parses as exp.ReadCSV and read_parquet() as exp.ReadParquet --
    # NOT exp.Anonymous -- so a name-only check would silently miss both.
    dangerous_nodes=frozenset({"ReadCSV", "ReadParquet", "Attach", "Detach", "Copy", "Export"}),
    prompt_notes=(
        "DuckDB does not report primary keys through reflection, so PK markers may be absent; "
        "do not assume a column is unique unless the schema says so."
    ),
    _build_uri=_duckdb_uri,
    _connect_args=lambda spec, config: {},
    _engine_kwargs=_duckdb_engine_kwargs,
)

# --------------------------------------------------------------------------- Trino


def _trino_http_scheme(config: Dict[str, Any]) -> str:
    """Default to HTTPS whenever a password is set -- basic auth over plain HTTP leaks it."""
    scheme = config.get("http_scheme")
    if scheme:
        return str(scheme).lower()
    return "https" if config.get("password") else "http"


def _trino_uri(spec: EngineSpec, config: Dict[str, Any]) -> URL:
    """Trino's dialect splits ``url.database`` on ``/`` into at most catalog + schema."""
    catalog = config.get("catalog") or config.get("database") or ""
    schema = config.get("schema") or ""
    database = f"{catalog}/{schema}" if catalog and schema else (catalog or None)
    return URL.create(
        drivername="trino",
        username=config.get("username") or None,
        password=config.get("password") or None,
        host=config.get("host") or "localhost",
        port=int(config.get("port") or spec.default_port),
        database=database,
    )


def _trino_connect_args(spec: EngineSpec, config: Dict[str, Any]) -> Dict[str, Any]:
    # Trino's DBAPI has no `connect_timeout`; it takes `request_timeout`.
    args: Dict[str, Any] = {"request_timeout": 30, "http_scheme": _trino_http_scheme(config)}
    verify = config.get("verify")
    if verify is not None and verify != "":
        # A CA bundle path stays a string; "true"/"false" become booleans.
        if isinstance(verify, str) and verify.lower() in ("true", "false"):
            args["verify"] = verify.lower() == "true"
        else:
            args["verify"] = verify
    return args


TRINO = EngineSpec(
    name="trino",
    display_name="Trino",
    sqlalchemy_url_driver="trino",
    sqlglot_dialect="trino",
    default_port=8080,
    pip_extra="trino",
    sql_name_parts=3,
    connection_fields=(
        "host",
        "port",
        "username",
        "password",
        "catalog",
        "schema",
        "http_scheme",
        "verify",
    ),
    quote_char='"',
    supports_transactions=False,
    supports_safe_dry_run=False,
    readonly_commands=frozenset({"SHOW", "DESCRIBE", "DESC"}),
    system_schemas=frozenset({"information_schema"}),
    cross_engine_via="configured Trino catalogs",
    prompt_notes=(
        "Trino exposes no foreign-key metadata: do not invent joins that the schema does not "
        "show. Use fully qualified catalog.schema.table names."
    ),
    _build_uri=_trino_uri,
    _connect_args=_trino_connect_args,
    # Trino speaks stateless HTTP; pooling buys nothing. Verified that {} leaves a QueuePool,
    # so NullPool must be explicit.
    _engine_kwargs=lambda spec, config: {"poolclass": NullPool},
)

# --------------------------------------------------------------------------- ClickHouse

CLICKHOUSE = EngineSpec(
    name="clickhouse",
    display_name="ClickHouse",
    sqlalchemy_url_driver="clickhousedb+connect",
    sqlglot_dialect="clickhouse",
    default_port=8123,
    pip_extra="clickhouse",
    sql_name_parts=2,
    connection_fields=("host", "port", "username", "password", "database"),
    quote_char="`",
    supports_transactions=False,
    supports_safe_dry_run=False,
    # SYSTEM and OPTIMIZE are deliberately absent: sqlglot parses them as the same Command
    # node as SHOW, but they mutate server state.
    readonly_commands=frozenset({"SHOW", "DESCRIBE", "DESC", "EXISTS"}),
    system_schemas=frozenset({"system", "information_schema", "INFORMATION_SCHEMA"}),
    # Stored lowercase: the check compares against node.name.lower(), so "remoteSecure"
    # as written would never match.
    dangerous_functions=frozenset(
        {"file", "url", "s3", "s3cluster", "remote", "remotesecure", "hdfs", "mysql", "postgresql", "jdbc", "odbc"}
    ),
    prompt_notes=(
        "ClickHouse exposes no foreign-key metadata: do not invent joins that the schema does "
        "not show. Prefer aggregate functions such as uniqExact over exact DISTINCT counts."
    ),
    _connect_args=lambda spec, config: {"connect_timeout": 10, "send_receive_timeout": 300},
    _engine_kwargs=lambda spec, config: {"pool_size": 5, "max_overflow": 5, "pool_recycle": 1800},
)
