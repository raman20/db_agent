"""The engine registry: name -> EngineSpec."""

from typing import Dict, List

from schemapilot.engines.analytics import CLICKHOUSE, DUCKDB, TRINO
from schemapilot.engines.base import EngineSpec
from schemapilot.engines.relational import MYSQL, POSTGRES, SQLITE

ENGINES: Dict[str, EngineSpec] = {
    spec.name: spec for spec in (POSTGRES, MYSQL, SQLITE, DUCKDB, TRINO, CLICKHOUSE)
}

#: Historical / colloquial names accepted from saved connection profiles and the CLI.
ALIASES: Dict[str, str] = {
    "postgresql": "postgres",
    "pg": "postgres",
    "mariadb": "mysql",
    "chdb": "clickhouse",
    "presto": "trino",
}


def get_spec(db_type: str) -> EngineSpec:
    """Resolve an engine name to its spec, raising a helpful error for unknown names."""
    key = (db_type or "").strip().lower()
    key = ALIASES.get(key, key)
    if key not in ENGINES:
        supported = ", ".join(sorted(ENGINES))
        raise ValueError(f"Unsupported engine '{db_type}'. Supported engines: {supported}.")
    return ENGINES[key]


def engine_names() -> List[str]:
    return sorted(ENGINES)


def driver_available(spec: EngineSpec) -> bool:
    """Whether the SQLAlchemy dialect for this engine can actually be loaded.

    Import is lazy and failure is not an error: a missing optional extra must never break
    ``import schemapilot``, it just means ``/engines`` shows the pip command to fix it.
    """
    try:
        from sqlalchemy.dialects import registry as sa_registry

        # SQLAlchemy registers "driver+dbapi" entry points under the dotted name "driver.dbapi".
        dialect_cls = sa_registry.load(spec.sqlalchemy_url_driver.replace("+", "."))
        # Loading the dialect class does not import its DBAPI module, so a missing
        # psycopg2/pymysql would otherwise report as available. Force the import.
        importer = getattr(dialect_cls, "import_dbapi", None) or getattr(dialect_cls, "dbapi", None)
        if callable(importer):
            importer()
        return True
    except Exception:
        return False
