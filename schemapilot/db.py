import json
import logging
import os
import stat
import tempfile
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import URL
from sqlalchemy.exc import NoSuchModuleError
from langchain_community.utilities.sql_database import SQLDatabase

from schemapilot.config import settings
from schemapilot.engines import EngineSpec, get_spec

logger = logging.getLogger("schemapilot.db")

# Store configuration files in the user's home configuration directory (standard global CLI practice)
USER_CONFIG_DIR = os.path.expanduser("~/.config/schemapilot")
CONNECTIONS_FILE = os.path.join(USER_CONFIG_DIR, "connections.json")

# connections.json and models.json hold plaintext database passwords and LLM API keys, so the
# directory is owner-only and the files are written 0600 instead of inheriting the process umask
# (which typically yields world-readable 0644). OS keyring storage is future work.
CONFIG_DIR_MODE = 0o700
CREDENTIAL_FILE_MODE = 0o600


def ensure_config_dir(directory: str = USER_CONFIG_DIR) -> str:
    """Creates the config directory 0700, tightening the mode if it already exists laxer."""
    os.makedirs(directory, mode=CONFIG_DIR_MODE, exist_ok=True)
    try:
        if stat.S_IMODE(os.stat(directory).st_mode) != CONFIG_DIR_MODE:
            os.chmod(directory, CONFIG_DIR_MODE)
    except OSError as e:  # pragma: no cover - platform/permission dependent
        logger.debug(f"Could not tighten permissions on {directory}: {e}")
    return directory


def write_credential_json(path: str, payload: Any):
    """Writes JSON credentials atomically with 0600 permissions.

    Atomic because a half-written connections.json is unrecoverable: the temp file lives in the
    same directory (so os.replace never crosses a filesystem boundary) and only replaces the
    original once it is completely flushed, leaving the previous file intact if we die midway.
    """
    directory = os.path.dirname(path) or "."
    ensure_config_dir(directory)

    fd, tmp_path = tempfile.mkstemp(prefix=".{}.".format(os.path.basename(path)), dir=directory)
    try:
        os.fchmod(fd, CREDENTIAL_FILE_MODE)
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f, indent=4)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except Exception:
        # Never leave the temp file behind; the original stays untouched.
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise


class DatabaseManager:
    """Manages dynamic database connectivity, raw execution, and metadata inspection.

    Everything engine-specific comes from the connection profile's :class:`EngineSpec`; this
    class contains no per-engine branching.
    """

    def __init__(self):
        self.engine = None
        self.langchain_db = None
        self.active_id = None
        self.connections = {}
        self.load_connections()
        self.auto_connect_active()

    # ------------------------------------------------------------------ profiles

    def load_connections(self):
        """Loads saved connections from the JSON store."""
        ensure_config_dir(os.path.dirname(CONNECTIONS_FILE))
        if os.path.exists(CONNECTIONS_FILE):
            try:
                with open(CONNECTIONS_FILE, "r") as f:
                    self.connections = json.load(f)
            except Exception as e:
                logger.error(f"Failed to load connections file: {e}")
                self.connections = {}
        else:
            self.connections = {}

    def save_connections(self):
        """Saves current connection registry to JSON (atomically, 0600)."""
        try:
            write_credential_json(CONNECTIONS_FILE, self.connections)
        except Exception as e:
            logger.error(f"Failed to save connections file: {e}")

    def add_connection(self, conn_id: str, config: Dict[str, Any]) -> str:
        """Saves connection profile config and returns the connection ID."""
        self.connections[conn_id] = config
        self.save_connections()
        return conn_id

    def delete_connection(self, conn_id: str):
        """Deletes a connection profile."""
        if conn_id in self.connections:
            del self.connections[conn_id]
            self.save_connections()
            if self.active_id == conn_id:
                self._dispose_engine()
                self.active_id = None

    def get_connections_list(self) -> List[Dict[str, Any]]:
        """Returns list of connection summaries."""
        return [
            {
                "id": cid,
                "name": cfg.get("name", cid),
                "db_type": cfg.get("db_type"),
                "host": cfg.get("host"),
                "port": cfg.get("port"),
                # `path` (file engines) and `catalog` (Trino) are optional keys; older profiles
                # only have `database`, so every accessor falls back.
                "path": cfg.get("path"),
                "catalog": cfg.get("catalog"),
                "schema": cfg.get("schema"),
                "database": cfg.get("database"),
                "username": cfg.get("username"),
                "is_active": self.active_id == cid,
            }
            for cid, cfg in self.connections.items()
        ]

    # ------------------------------------------------------------------ engine lifecycle

    @staticmethod
    def spec_for(config: Dict[str, Any]) -> EngineSpec:
        """Resolves the EngineSpec for a profile; unknown engines raise a naming ValueError."""
        return get_spec(config.get("db_type") or settings.DB_TYPE)

    @property
    def active_spec(self) -> Optional[EngineSpec]:
        """The EngineSpec of the active connection, or None when nothing is connected."""
        if not self.active_id or self.active_id not in self.connections:
            return None
        return self.spec_for(self.connections[self.active_id])

    @property
    def active_config(self) -> Dict[str, Any]:
        if not self.active_id:
            return {}
        return self.connections.get(self.active_id, {})

    def build_uri(self, config: Dict[str, Any]) -> URL:
        """Builds the SQLAlchemy URL for a profile via its spec.

        Delegation matters for correctness, not just tidiness: the specs use ``URL.create()``,
        whereas the f-string interpolation this replaced turned the password ``p@ss/w:rd`` into
        password ``p`` and host ``ss`` -- a silent connection to the wrong server.
        """
        return self.spec_for(config).build_uri(config)

    def _create_engine(self, config: Dict[str, Any], spec: EngineSpec):
        """Creates an engine from spec-supplied connect_args/engine_kwargs."""
        uri = spec.build_uri(config)
        try:
            return create_engine(
                uri,
                connect_args=spec.connect_args(config),
                **spec.engine_kwargs(config),
            )
        except NoSuchModuleError as e:
            # A missing optional driver is a setup problem, not a bug: answer with the exact
            # pip command instead of a SQLAlchemy stack trace.
            hint = spec.install_hint()
            message = f"{spec.display_name} driver is not installed"
            raise ConnectionError(f"{message}. {hint}" if hint else f"{message}: {e}")

    def _probe(self, engine, spec: EngineSpec):
        """Runs SELECT 1, committing only on engines where a transaction actually exists."""
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
            # Trino and ClickHouse have no transactions; committing raises or is meaningless.
            if spec.supports_transactions:
                conn.commit()

    def _dispose_engine(self):
        """Releases the pooled connections of the current engine, if any."""
        if self.engine is not None:
            try:
                self.engine.dispose()
            except Exception as e:  # pragma: no cover - dispose rarely fails
                logger.debug(f"Engine dispose failed: {e}")
        self.engine = None
        self.langchain_db = None

    def test_connection(self, config: Dict[str, Any]) -> Tuple[bool, str]:
        """Tests database connection parameters without saving them."""
        test_engine = None
        try:
            spec = self.spec_for(config)
            test_engine = self._create_engine(config, spec)
            self._probe(test_engine, spec)
            return True, "Connection successful"
        except Exception as e:
            return False, str(e)
        finally:
            # The throwaway engine holds a pool of real sockets / an open DuckDB file.
            if test_engine is not None:
                try:
                    test_engine.dispose()
                except Exception:  # pragma: no cover
                    pass

    def select_connection(self, conn_id: str, persist: bool = True) -> bool:
        """Sets the active connection and initializes the engines."""
        if conn_id not in self.connections:
            return False

        config = self.connections[conn_id]
        spec = self.spec_for(config)

        # Drop the previous engine's pool before replacing it, otherwise switching connections
        # leaks a live pool per switch for the lifetime of the process.
        self._dispose_engine()

        try:
            logger.info(f"Connecting to database '{conn_id}' using engine: {spec.name}")
            self.engine = self._create_engine(config, spec)
            self._probe(self.engine, spec)

            self.langchain_db = SQLDatabase(
                self.engine,
                sample_rows_in_table_info=3,
                max_string_length=1000,
            )
            self.active_id = conn_id
            if persist:
                self._persist_active(conn_id)
            logger.info(f"Switched active database to: {conn_id}")
            return True
        except ConnectionError:
            self._dispose_engine()
            raise
        except Exception as e:
            self._dispose_engine()
            # `e` can embed the URI; specs build URLs whose repr masks the password, but never
            # log the raw config.
            logger.error(f"Failed to connect to database '{conn_id}': {e}")
            raise ConnectionError(f"Database connection failed: {e}")

    def _persist_active(self, conn_id: str):
        """Records which profile is active so the next process starts on the same database."""
        for cid, cfg in self.connections.items():
            cfg["is_active"] = cid == conn_id
        self.save_connections()

    def auto_connect_active(self):
        """Connects to the PERSISTED active profile at startup.

        The previous implementation took the first dict entry and ignored the persisted flag, so
        the CLI silently came up pointed at a different database than the user last selected.
        Falls back to the first profile only when no profile is flagged (same convention as
        ModelProfileManager's `is_active`).
        """
        if not self.connections:
            return

        target = next((cid for cid, cfg in self.connections.items() if cfg.get("is_active")), None)
        if target is None:
            target = next(iter(self.connections))

        try:
            # persist=False: startup must not rewrite the credentials file as a side effect.
            self.select_connection(target, persist=False)
        except Exception as e:
            logger.warning(f"Could not auto-connect to '{target}': {e}")

    # ------------------------------------------------------------------ queries

    def get_tables(self) -> List[str]:
        """Gets list of tables for active connection."""
        if not self.langchain_db:
            return []
        return self.langchain_db.get_usable_table_names()

    def execute_query(self, query: str) -> Dict[str, Any]:
        """Executes a query on active connection and returns structured results."""
        if not self.engine:
            raise ConnectionError("No active database connection selected")

        spec = self.active_spec
        with self.engine.connect() as conn:
            result = conn.execute(text(query))

            if not result.returns_rows:
                if spec is None or spec.supports_transactions:
                    conn.commit()
                return {"message": "Query executed successfully. No rows returned.", "row_count": result.rowcount}

            columns = list(result.keys())
            rows = [dict(zip(columns, row)) for row in result.fetchall()]
            return {"columns": columns, "rows": rows}

    # ------------------------------------------------------------------ introspection

    def _resolve_schemas(self, inspector, spec: EngineSpec, config: Dict[str, Any],
                         schemas: Optional[List[str]]) -> List[Optional[str]]:
        """Decides which schemas to reflect, in a deliberate order of precedence.

        1. an explicit `schemas=` argument,
        2. else the schema configured on the connection profile,
        3. else enumerate `get_schema_names()` (only meaningful when names have >= 2 parts).

        The order is load-bearing, not cosmetic: Trino's `tpch` catalog exposes `sf1`, `sf100`
        and `sf1000` next to `tiny`, so blind enumeration burns the whole table budget on
        schemas the user never asked about and never reaches the configured one.
        """
        if schemas:
            return [s for s in schemas if s]

        if spec.sql_name_parts >= 2:
            configured = config.get("schema")
            if configured:
                return [configured]
            return self._enumerate_schemas(inspector, spec)

        # Single-part names (SQLite): there is no schema to pass.
        return [None]

    def _enumerate_schemas(self, inspector, spec: EngineSpec) -> List[Optional[str]]:
        """Lists user schemas, filtering the engine's own system schemas."""
        try:
            names = inspector.get_schema_names()
        except Exception as e:
            logger.warning(f"Could not enumerate schemas: {e}")
            return [None]

        system = {s.lower() for s in spec.system_schemas}
        keep: List[Optional[str]] = []
        for name in names:
            if spec.schema_name_style == "composite":
                # DuckDB reports `db.schema` pairs -- verified output:
                # ['dtest.main', 'dtest.other', 'system.information_schema', 'system.main',
                #  'temp.main']. Split on the LAST dot (a database name may itself contain
                # dots) and reject the pair if either half is a system name, which is what
                # drops the system.* / temp.* entries. The composite string is kept verbatim
                # because that is exactly what the inspector accepts as `schema=`.
                database, _, schema = name.rpartition(".")
                if database.lower() in system or schema.lower() in system:
                    continue
            elif name.lower() in system:
                continue
            keep.append(name)

        return keep or [None]

    @staticmethod
    def _qualify(spec: EngineSpec, config: Dict[str, Any], schema: Optional[str], table: str) -> str:
        """Builds the name the LLM should actually write: schema-qualified where it applies."""
        if not schema:
            return table
        qualified = f"{schema}.{table}"
        # Trino needs catalog.schema.table, but its catalog lives in the connection profile
        # rather than in get_schema_names() (unlike DuckDB's composite `db.schema`, which
        # already carries it -- hence the dot check).
        catalog = config.get("catalog")
        if spec.sql_name_parts >= 3 and catalog and "." not in schema:
            qualified = f"{catalog}.{qualified}"
        return qualified

    def get_schema_metadata(self, schemas: Optional[List[str]] = None,
                            max_tables: Optional[int] = None) -> Dict[str, Any]:
        """Extracts schema definitions for the active connection.

        Returns ``{"tables": {qualified_name: {...}}, "relationships": [...],
        "truncated": bool, "total_tables": int, "skipped_tables": [...],
        "reflection_failed": bool}``.
        """
        if not self.engine:
            raise ConnectionError("No active database connection selected")

        spec = self.active_spec
        config = self.active_config
        cap = max_tables if max_tables is not None else settings.CATALOG_MAX_TABLES

        # Bind the inspector to a checked-out Connection, NOT to the Engine. Verified against a
        # live ClickHouse: clickhouse-connect's inspector still calls the SQLAlchemy 1.x
        # `bind.execute()`, which a Connection has and an Engine does not in 2.x, so
        # inspect(engine).get_columns(...) raised AttributeError for every table and the whole
        # catalog came back empty. A Connection satisfies both the 1.x-style and 2.x dialects.
        with self.engine.connect() as conn:
            return self._reflect(conn, spec, config, schemas, cap)

    def _reflect(self, conn, spec: EngineSpec, config: Dict[str, Any],
                 schemas: Optional[List[str]], cap: int) -> Dict[str, Any]:
        """Reflects the catalog over an already-open connection."""
        inspector = inspect(conn)
        target_schemas = self._resolve_schemas(inspector, spec, config, schemas)

        # Collect the full (schema, table) inventory first so `total_tables` reports what exists
        # rather than what fitted inside the cap.
        inventory: List[Tuple[Optional[str], str]] = []
        for schema in target_schemas:
            try:
                for table in inspector.get_table_names(schema=schema):
                    inventory.append((schema, table))
            except Exception as e:
                logger.warning(f"Could not list tables in schema '{schema}': {e}")

        total_tables = len(inventory)
        truncated = total_tables > cap

        tables_metadata: Dict[str, Any] = {}
        relationships: List[Dict[str, Any]] = []
        skipped: List[str] = []

        attempted = inventory[:cap]
        for schema, table in attempted:
            qualified = self._qualify(spec, config, schema, table)
            try:
                # get_pk_constraint(), NOT get_primary_keys(): the latter was removed before
                # SQLAlchemy 2.0, which made this whole method raise AttributeError.
                pk_cols = inspector.get_pk_constraint(table, schema=schema).get("constrained_columns") or []

                columns_info = [
                    {
                        "name": col["name"],
                        "type": str(col["type"]),
                        "nullable": col.get("nullable", True),
                        "is_primary": col["name"] in pk_cols,
                    }
                    for col in inspector.get_columns(table, schema=schema)
                ]

                tables_metadata[qualified] = {
                    "name": qualified,
                    "schema": schema,
                    "table": table,
                    "columns": columns_info,
                }

                # Extract foreign keys for relationship mapping. ClickHouse and Trino report
                # none at all, so an empty list is normal rather than an error.
                for fk in inspector.get_foreign_keys(table, schema=schema):
                    referred_schema = fk.get("referred_schema") or schema
                    relationships.append({
                        "from_table": qualified,
                        "from_columns": fk.get("constrained_columns") or [],
                        "to_table": self._qualify(spec, config, referred_schema, fk.get("referred_table") or ""),
                        "to_columns": fk.get("referred_columns") or [],
                    })
            except Exception as e:
                # One unreadable table (permissions, an exotic type, a view over a dead source)
                # must not cost the user the entire catalog.
                logger.warning(f"Skipping table '{qualified}': {e}")
                skipped.append(qualified)
                continue

        # A wholesale reflection failure looks identical to an empty database unless we say so.
        # That is exactly how the ClickHouse inspect(engine) bug hid: every table hit the
        # tolerant per-table handler above, so the caller got a clean-looking empty catalog with
        # a correct total_tables and no complaint. Losing EVERY table is a defect, not tolerable
        # attrition, so it is logged at error level and flagged in the result.
        reflection_failed = bool(attempted) and not tables_metadata
        if reflection_failed:
            logger.error(
                f"Reflection failed for all {len(attempted)} table(s) on engine "
                f"'{spec.name}' -- returning an empty catalog. First failure above; "
                f"tables: {', '.join(skipped[:5])}"
                + (" ..." if len(skipped) > 5 else "")
            )
        elif skipped:
            logger.warning(
                f"Reflected {len(tables_metadata)} of {len(attempted)} table(s); "
                f"skipped: {', '.join(skipped)}"
            )

        return {
            "tables": tables_metadata,
            "relationships": relationships,
            "truncated": truncated,
            "total_tables": total_tables,
            "skipped_tables": skipped,
            "reflection_failed": reflection_failed,
        }

# Singleton instance
_db_manager = None

def get_db() -> DatabaseManager:
    global _db_manager
    if _db_manager is None:
        _db_manager = DatabaseManager()
    return _db_manager
