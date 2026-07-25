"""The schema catalog: one in-memory cache, one owner.

``SchemaCache`` is the **sole owner of schema metadata** in SchemaPilot. Nothing else caches
introspection results -- an earlier design had both ``DatabaseManager`` and a cache holding
tables, which is two sources of truth and therefore two ways to show the user a table that no
longer exists.

Deliberately minimal in v1:

* **In memory only.** No JSON file on disk, no TTL, no invalidation timer. Persistence buys
  little for a process that is usually alive for one session, and a stale on-disk catalog is a
  wrong-answer generator.
* **No background warming.** Warming runs synchronously (``/use`` does it under a Rich status so
  the user sees a spinner rather than a freeze). A warm thread would race ``/use``: ``/use``
  mutates ``db.engine`` / ``db.active_id`` / ``db.active_spec`` while the thread is halfway
  through introspecting the *previous* connection, so the results land under the new connection
  id. Locking that correctly costs more than the freeze it would avoid.
* **Keyed by connection id.** Every read checks that the cached metadata belongs to
  ``db.active_id``; switching connections therefore cannot serve the previous database's tables.
"""

import fnmatch
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger("schemapilot.catalog")


class SchemaCache:
    """In-memory schema metadata for the active connection.

    Reads go through :meth:`warm` semantics: the cache is populated on first use and on explicit
    :meth:`refresh`, never implicitly re-fetched behind the user's back, because the callers that
    must not block (the completer) and the callers that may block (the commands) need different
    behaviour and only the latter is allowed to perform I/O.
    """

    def __init__(self, db, max_tables: Optional[int] = None):
        self.db = db
        self.max_tables = max_tables
        self._metadata: Optional[Dict[str, Any]] = None
        #: The connection the cached metadata came from; compared against db.active_id on read.
        self._connection_id: Optional[str] = None
        #: Column -> referenced target, per qualified table, derived from `relationships`.
        self._fk_index: Dict[str, Dict[str, str]] = {}

    # ------------------------------------------------------------------ state

    @property
    def connection_id(self) -> Optional[str]:
        return self._connection_id

    @property
    def is_warm(self) -> bool:
        """True only when metadata is loaded AND belongs to the currently active connection."""
        return self._metadata is not None and self._connection_id == self.db.active_id

    @property
    def truncated(self) -> bool:
        """Whether introspection hit ``CATALOG_MAX_TABLES`` and stopped short."""
        return bool(self._metadata.get("truncated")) if self.is_warm else False

    @property
    def total_tables(self) -> int:
        """How many tables exist, which can exceed how many were reflected (see truncated)."""
        return int(self._metadata.get("total_tables", 0)) if self.is_warm else 0

    def invalidate(self):
        """Forgets the cached metadata (called on connection switch)."""
        self._metadata = None
        self._connection_id = None
        self._fk_index = {}

    # ------------------------------------------------------------------ loading

    def warm(self, force: bool = False) -> Dict[str, Any]:
        """Loads metadata if needed and returns it. Blocking -- never call from the completer.

        Raises ``ConnectionError`` when there is nothing to introspect, so callers can print the
        one-action fix instead of showing an empty catalog that looks like an empty database.
        """
        if self.is_warm and not force:
            return self._metadata

        if not self.db.active_id:
            raise ConnectionError("No active database connection selected")

        metadata = self.db.get_schema_metadata(max_tables=self.max_tables)
        self._metadata = metadata
        # Recorded AFTER the fetch: if introspection raises we keep no half-built catalog, and
        # the id we store is the connection the data actually came from.
        self._connection_id = self.db.active_id
        self._fk_index = self._build_fk_index(metadata)
        return metadata

    def refresh(self) -> Dict[str, Any]:
        """Re-introspects the active connection unconditionally."""
        return self.warm(force=True)

    @staticmethod
    def _build_fk_index(metadata: Dict[str, Any]) -> Dict[str, Dict[str, str]]:
        """Flattens `relationships` into per-table column -> target, for the FK markers.

        ClickHouse and Trino report no foreign keys at all, so an empty index is normal.
        """
        index: Dict[str, Dict[str, str]] = {}
        for rel in metadata.get("relationships") or []:
            from_table = rel.get("from_table")
            to_table = rel.get("to_table") or "?"
            from_columns = rel.get("from_columns") or []
            to_columns = rel.get("to_columns") or []
            if not from_table:
                continue
            per_table = index.setdefault(from_table, {})
            for position, column in enumerate(from_columns):
                target_column = to_columns[position] if position < len(to_columns) else ""
                per_table[column] = f"{to_table}.{target_column}" if target_column else to_table
        return index

    # ------------------------------------------------------------------ reads (never I/O)

    def table_names(self) -> List[str]:
        """Qualified table names, sorted. Empty when cold -- callers must not treat that as
        'the database has no tables'; check :attr:`is_warm` first."""
        if not self.is_warm:
            return []
        return sorted(self._metadata.get("tables") or {})

    def tables_by_schema(self) -> Dict[str, List[str]]:
        """Groups qualified names by their schema, for the ``/schema`` tree.

        Single-part engines (SQLite) have no schema; those tables are grouped under ``""`` and
        rendered directly under the connection node.
        """
        grouped: Dict[str, List[str]] = {}
        if not self.is_warm:
            return grouped
        for qualified, info in (self._metadata.get("tables") or {}).items():
            grouped.setdefault(info.get("schema") or "", []).append(qualified)
        for names in grouped.values():
            names.sort()
        return grouped

    def table(self, name: str) -> Optional[Dict[str, Any]]:
        """The raw metadata entry for a table, resolving unqualified names."""
        resolved = self.resolve(name)
        if resolved is None:
            return None
        return (self._metadata.get("tables") or {}).get(resolved)

    def columns(self, name: str) -> List[Dict[str, Any]]:
        """Columns of a table, each annotated with ``is_primary`` and ``foreign_key``."""
        info = self.table(name)
        if not info:
            return []
        resolved = self.resolve(name)
        fks = self._fk_index.get(resolved, {})
        return [
            {
                "name": column.get("name"),
                "type": column.get("type"),
                "nullable": column.get("nullable", True),
                "is_primary": bool(column.get("is_primary")),
                "foreign_key": fks.get(column.get("name")),
            }
            for column in info.get("columns") or []
        ]

    def relationships(self) -> List[Dict[str, Any]]:
        if not self.is_warm:
            return []
        return list(self._metadata.get("relationships") or [])

    def resolve(self, name: str) -> Optional[str]:
        """Maps user input to a qualified table name.

        Accepts the qualified name, the bare table name, or any dotted suffix, because the user
        types ``@orders`` while the catalog is keyed ``tpch.tiny.orders``. Case-insensitive;
        ambiguity resolves to the alphabetically first match so behaviour is deterministic.
        """
        if not name or not self.is_warm:
            return None
        needle = name.strip().strip('"').strip("`").lower()
        if not needle:
            return None

        candidates = self.table_names()
        for qualified in candidates:
            if qualified.lower() == needle:
                return qualified
        for qualified in candidates:
            if qualified.lower().endswith("." + needle):
                return qualified
        return None

    def match(self, pattern: Optional[str] = None) -> List[str]:
        """Table names filtered by a shell glob (``/tables sales_*``).

        A pattern with no wildcard is treated as a substring so ``/tables order`` is useful
        without the user having to write ``*order*``.
        """
        names = self.table_names()
        if not pattern:
            return names
        needle = pattern.strip().lower()
        if not needle:
            return names
        if any(character in needle for character in "*?["):
            return [n for n in names if fnmatch.fnmatch(n.lower(), needle)]
        return [n for n in names if needle in n.lower()]

    def summary(self) -> str:
        """One-line status for the REPL header, honest about a cold cache."""
        if not self.is_warm:
            return "catalog not loaded"
        shown = len(self.table_names())
        if self.truncated:
            return f"{shown} of {self.total_tables} tables cached"
        return f"{shown} tables cached"
