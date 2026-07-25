"""The query lifecycle: schema selection -> SQL generation -> sentry -> execution -> analysis.

Two things dominate the cost and the correctness of that lifecycle, and both used to be wrong:

* **What schema the model sees.** This module used to send ``langchain_db.get_table_info()`` --
  full ``CREATE TABLE`` DDL for *every* table plus three real data rows each -- on every single
  question. On a wide catalog that is hundreds of thousands of tokens, most of them about tables
  the question never mentions, and the sample rows are live customer data leaving the machine.
  Now: a compact ``col: type`` rendering of :meth:`DatabaseManager.get_schema_metadata`,
  restricted to the tables :mod:`schemapilot.pruning` selected, with samples opt-in per
  connection.
* **Which dialect the model writes.** The dialect came from ``db.engine.dialect.name``, which
  reports ``clickhousedb`` for clickhouse-connect and ``postgresql`` for Postgres -- neither a
  registry key nor a sqlglot dialect. It now comes from the frozen ``EngineSpec``, which is also
  what tells the sentry which per-engine policy to apply.
"""

import json
import logging
from typing import Any, AsyncGenerator, Dict, List, Optional, Tuple

from langchain_core.messages import HumanMessage

from schemapilot.config import settings
from schemapilot.db import get_db
from schemapilot.llm import get_llm
from schemapilot.pruning import select_relevant_tables
from schemapilot.security import validate_sql_query

logger = logging.getLogger("schemapilot.agent")

#: Rows fetched per shortlisted table when sampling is opted into. Three is enough to show the
#: shape of an enum-ish column and small enough to stay cheap.
SAMPLE_ROW_LIMIT = 3

#: Connections already warned about sample egress, so the warning appears once per process
#: rather than on every question. Keyed by (connection id, LLM target) because switching either
#: means the user is sending rows somewhere new and deserves to be told again.
_sample_warnings_emitted = set()


def reset_sample_warnings():
    """Clear the one-time sample-egress warning state (used by tests)."""
    _sample_warnings_emitted.clear()


def table_name_parts(spec, qualified: str, meta: Optional[Dict[str, Any]] = None) -> List[str]:
    """Recover the individual identifiers of a table from its metadata entry.

    The catalog is *keyed* by a dotted display name, but a dotted display name is ambiguous:
    a SQLite table literally called ``sales.2024`` and a Postgres table ``sales`` in schema
    ``2024`` produce the same string. Splitting the key on dots therefore either quotes one
    identifier as two (sampling then fails) or, worse, resolves to a different object. So the
    parts come from the components ``get_schema_metadata()`` already records per table --
    ``schema`` and ``table`` -- and only the leading catalog segment, which lives nowhere else,
    is recovered from the key by removing the known ``schema.table`` suffix.

    Two engine quirks are handled here rather than at the call site:

    * DuckDB reports COMPOSITE schemas (``dtest.main``), which are one metadata field but two
      SQL identifiers, so a composite schema is split on its last dot.
    * Trino's catalog is not part of ``get_schema_names()`` output at all; it is prefixed onto
      the display key by ``db._qualify``, which is why it is recovered from the key.
    """
    table = (meta or {}).get("table")
    if not table:
        # Metadata without split components (hand-built dicts, older callers). The display key
        # is all we have, so fall back to its dotted reading -- documented, not preferred.
        return [p for p in str(qualified).split(".") if p]

    schema = (meta or {}).get("schema")
    parts: List[str] = []

    suffix = f"{schema}.{table}" if schema else table
    if qualified.endswith(suffix) and len(qualified) > len(suffix):
        catalog_part = qualified[: -len(suffix)].rstrip(".")
        if catalog_part:
            parts.append(catalog_part)

    if schema:
        if spec is not None and spec.schema_name_style == "composite":
            database, _, leaf = schema.rpartition(".")
            if database:
                parts.append(database)
            parts.append(leaf or schema)
        else:
            parts.append(schema)

    parts.append(table)
    return parts


def quote_identifier(spec, parts: List[str]) -> str:
    """Quote each identifier with the engine's quote character and join with dots.

    Never f-string a raw catalog name into SQL: a name containing a space, a keyword or a quote
    character either breaks the statement or changes what it means. An embedded quote character
    is doubled, which is how every engine here escapes it.
    """
    quote = spec.quote_char if spec is not None else '"'
    return ".".join(f"{quote}{p.replace(quote, quote * 2)}{quote}" for p in parts)


def build_sample_query(spec, qualified: str, meta: Optional[Dict[str, Any]] = None,
                       limit: int = SAMPLE_ROW_LIMIT) -> str:
    """``SELECT * FROM <safely quoted table> LIMIT n`` in the engine's limit syntax."""
    limit_clause = (spec.row_limit_clause if spec is not None else "LIMIT {n}").format(n=limit)
    identifier = quote_identifier(spec, table_name_parts(spec, qualified, meta))
    return f"SELECT * FROM {identifier} {limit_clause}"


def llm_target_label(llm_config: Optional[Dict[str, Any]] = None) -> str:
    """Human-readable "provider/model" the rows would be sent to, for the sample warning.

    A warning that says "samples will be sent to the LLM" is useless; the user needs to know
    *which* third party is about to receive their rows.
    """
    config: Dict[str, Any] = {}
    try:
        from schemapilot.llm import get_model_manager

        config.update(get_model_manager().get_active_profile() or {})
    except Exception as e:  # pragma: no cover - profile store unreadable
        logger.debug(f"Could not resolve active model profile for the sample warning: {e}")

    for key in ("provider", "model_name"):
        value = (llm_config or {}).get(key) or (llm_config or {}).get(f"llm_{key}")
        if value:
            config[key] = value

    provider = config.get("provider") or settings.LLM_PROVIDER or "the configured LLM provider"
    model = config.get("model_name") or settings.LLM_MODEL_NAME
    return f"{provider} ({model})" if model else str(provider)


def render_schema(catalog: Dict[str, Any], tables: List[str],
                  samples: Optional[Dict[str, Dict[str, Any]]] = None) -> str:
    """Render the selected tables compactly: qualified name, ``col: type``, PK marker, FK arrows.

    Deliberately not DDL. The model needs names, types and how to join; ``CREATE TABLE`` adds
    storage clauses, collations, constraint syntax and engine options that change no answer and
    multiply the token count.
    """
    all_tables = catalog.get("tables", {}) or {}
    lines: List[str] = []

    for qualified in tables:
        meta = all_tables.get(qualified)
        if not meta:
            continue
        lines.append(f"TABLE {qualified}")
        for col in meta.get("columns", []) or []:
            marker = " PK" if col.get("is_primary") else ""
            null_marker = "" if col.get("nullable", True) else " NOT NULL"
            lines.append(f"  {col.get('name')}: {col.get('type')}{marker}{null_marker}")

        sample = (samples or {}).get(qualified)
        if sample and sample.get("rows"):
            lines.append(f"  -- sample rows ({len(sample['rows'])}):")
            for row in sample["rows"]:
                lines.append(f"  --   {json.dumps(row, default=str)}")

    # FK arrows are listed once, after the tables, and only for edges whose BOTH ends are in the
    # prompt -- an arrow to a table the model cannot see is an invitation to invent it.
    selected = set(tables)
    arrows = []
    for rel in catalog.get("relationships", []) or []:
        if rel.get("from_table") in selected and rel.get("to_table") in selected:
            from_cols = ", ".join(rel.get("from_columns") or [])
            to_cols = ", ".join(rel.get("to_columns") or [])
            arrows.append(f"  {rel['from_table']}({from_cols}) -> {rel['to_table']}({to_cols})")
    if arrows:
        lines.append("RELATIONSHIPS (foreign keys)")
        lines.extend(arrows)

    if catalog.get("truncated"):
        lines.append(
            f"-- NOTE: this database has {catalog.get('total_tables')} tables; only part of the "
            "catalog was reflected."
        )
    if len(selected) < len(all_tables):
        lines.append(
            f"-- NOTE: {len(selected)} of {len(all_tables)} known tables are shown, chosen for "
            "this question. If the table you need is missing, say so instead of guessing."
        )

    return "\n".join(lines)


class SchemaPilotAgent:
    """Generates, validates, executes and summarises one SQL query per call to :meth:`execute`."""

    def __init__(self):
        self.llm = None

    def _get_text_content(self, content: Any) -> str:
        """Safely extracts string content from response to handle multi-part lists in newer LangChains."""
        if isinstance(content, list):
            parts = []
            for part in content:
                if isinstance(part, str):
                    parts.append(part)
                elif isinstance(part, dict) and "text" in part:
                    parts.append(part["text"])
                elif hasattr(part, "text"):
                    parts.append(part.text)
            return "".join(parts)
        elif not isinstance(content, str):
            return str(content)
        return content

    # ------------------------------------------------------------------ prompts

    def build_generation_prompt(self, query: str, schemas: str, spec,
                                previous_error: Optional[str] = None) -> str:
        """Assembles the SQL-generation prompt: dialect, engine notes, and the read-only rule."""
        dialect = spec.sqlglot_dialect if spec is not None else "mysql"

        rules = [
            "Output ONLY the raw SQL query. Do not wrap it in markdown or backticks.",
            "This session is READ-ONLY: write a single SELECT statement. INSERT, UPDATE, DELETE, "
            "DDL and administrative statements are rejected before execution, so do not propose "
            "them -- if the question needs one, explain that instead of emitting SQL.",
            "Reference tables and columns exactly as written in the schema above; do not invent "
            "names.",
            "Apply a limit (max 100) if the question does not specify one.",
        ]
        if spec is not None and spec.sql_name_parts == 3:
            # Trino and DuckDB resolve names against a catalog; a bare schema.table only works
            # when a session default happens to match, which is exactly the class of failure the
            # self-correction loop burns attempts on.
            rules.append(
                "Names on this engine have THREE parts: always write "
                "catalog.schema.table exactly as shown in the schema."
            )

        numbered = "\n".join(f"{i}. {rule}" for i, rule in enumerate(rules, start=1))

        engine_notes = ""
        if spec is not None and spec.prompt_notes:
            engine_notes = f"\nEngine notes: {spec.prompt_notes}\n"

        repair_context = ""
        if previous_error:
            repair_context = (
                f"\n\n--- CRITICAL: SELF-CORRECTION REQUIRED ---\n"
                f"Your previous SQL query failed with error: {previous_error}\n"
                "Please analyze the error, review column/table names, and output a corrected SQL query."
            )

        return (
            f"You are SchemaPilot, an expert database assistant translating natural language to "
            f"{dialect} SQL.\n"
            f"Database Schema:\n{schemas}\n"
            f"{engine_notes}\n"
            f"User Question: '{query}'{repair_context}\n\n"
            f"Generate a clean, optimized SQL SELECT statement to retrieve the answer.\n"
            f"Rules:\n{numbered}"
        )

    async def generate_sql(self, query: str, schemas: str, spec,
                           previous_error: Optional[str] = None) -> str:
        """Asks the LLM to write a clean SQL query. Handles self-correction on database errors."""
        prompt = self.build_generation_prompt(query, schemas, spec, previous_error)
        response = await self.llm.ainvoke([HumanMessage(content=prompt)])
        text_content = self._get_text_content(response.content)
        return text_content.strip().replace("```sql", "").replace("```", "").strip()

    def build_analysis_prompt(self, query: str, sql: str, data: Dict[str, Any]) -> str:
        """Assembles the analyst prompt with a capped row sample, not the whole result set.

        The previous version sent ``json.dumps(data)`` -- the entire result set, up to
        ``MAX_QUERY_LIMIT`` rows -- and then asked for a Recharts config that embedded *another*
        copy of those rows, which no caller ever read. A summary needs a sample and the true row
        count; anything beyond that is paid-for noise.
        """
        rows = data.get("rows") if isinstance(data, dict) else None
        if rows is None:
            payload = json.dumps(data, default=str)
            row_note = ""
        else:
            cap = max(1, int(settings.ANALYST_SAMPLE_ROWS))
            sample = rows[:cap]
            payload = json.dumps(
                {"columns": data.get("columns", []), "rows": sample}, default=str
            )
            row_note = f"Total rows returned: {len(rows)}"
            if len(rows) > len(sample):
                row_note += f" (only the first {len(sample)} are shown below)"
            row_note += "\n"

        return (
            "You are a database analyst. Review the user's question, the executed SQL, and the "
            "resulting dataset.\n"
            f"Question: '{query}'\n"
            f"Executed SQL: {sql}\n"
            f"{row_note}"
            f"Dataset (JSON): {payload}\n\n"
            "Provide a clear natural language summary of the dataset that answers the question. "
            "Format tabular results as markdown tables. Reply with the summary text only -- no "
            "JSON wrapper, no code fences."
        )

    async def analyze_and_format(self, query: str, sql: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """Summarises the result set in natural language. Returns ``{"summary": str}``."""
        prompt = self.build_analysis_prompt(query, sql, data)
        try:
            response = await self.llm.ainvoke([HumanMessage(content=prompt)])
            summary = self._get_text_content(response.content).strip()
            # Older prompts asked for JSON; a model that still answers that way should not leak
            # braces into the terminal.
            if summary.startswith("{"):
                try:
                    parsed = json.loads(summary.replace("```json", "").replace("```", "").strip())
                    summary = str(parsed.get("summary", summary))
                except Exception:
                    pass
            return {"summary": summary}
        except Exception as e:
            logger.error(f"Analysis failed: {e}")
            return {"summary": f"Query returned results: {str(data)}"}

    # ------------------------------------------------------------------ sampling

    def collect_samples(self, db, spec, tables: List[str],
                        catalog: Optional[Dict[str, Any]] = None) -> Tuple[Dict[str, Dict[str, Any]], List[str]]:
        """Fetch up to :data:`SAMPLE_ROW_LIMIT` rows for each SHORTLISTED table.

        Sampling is opt-in (``include_samples: true`` on the connection profile) because the rows
        are real production data being handed to a third-party model. The previous behaviour --
        three rows of every table on every question, via ``get_table_info()`` -- was an
        unannounced default. Only shortlisted tables are sampled: the tables that are not in the
        prompt cannot help the model, so reading their data would be pure egress.

        Returns ``(samples, executed_sql)``; the SQL list is returned so the caller (and tests)
        can see exactly what was run.
        """
        samples: Dict[str, Dict[str, Any]] = {}
        executed: List[str] = []
        all_tables = (catalog or {}).get("tables", {}) or {}
        for qualified in tables:
            # The metadata entry, not the dotted key, is what makes the identifier unambiguous.
            sql = build_sample_query(spec, qualified, all_tables.get(qualified), SAMPLE_ROW_LIMIT)
            executed.append(sql)
            try:
                result = db.execute_query(sql)
            except Exception as e:
                # A permission-denied or exotic-type table must not sink the whole question.
                logger.warning(f"Could not sample '{qualified}': {e}")
                continue
            rows = result.get("rows") if isinstance(result, dict) else None
            if rows:
                samples[qualified] = {"rows": rows[:SAMPLE_ROW_LIMIT]}
        return samples, executed

    # ------------------------------------------------------------------ lifecycle

    async def execute(self, query: str, history: List[Dict[str, Any]] = None,
                      llm_config: Dict[str, Any] = None,
                      pinned_tables: List[str] = None,
                      catalog: Dict[str, Any] = None) -> AsyncGenerator[str, None]:
        """Runs the query lifecycle and streams newline-delimited JSON events.

        Args:
            query: the natural-language question.
            history: prior turns, for context.
            llm_config: per-call model overrides.
            pinned_tables: qualified names the user pinned with ``@table``; always included.
            catalog: already-materialised ``get_schema_metadata()`` output. Supply it and the
                agent introspects NOTHING -- this is how the REPL hands over its warmed
                ``SchemaCache`` so the completer, ``/schema``, ``@`` pin resolution and the LLM
                prompt all describe the same catalog, and so a warehouse is introspected once per
                ``/use`` rather than once per question. Omitted (the one-shot
                ``schemapilot "question"`` path, and any direct library caller) the agent fetches
                it itself, so this stays backward compatible.

        Events: ``agent_message``, ``schema_selection`` (what pruning chose, for ``/why``),
        ``final_output``, ``swarm_completed``, ``error``.
        """
        try:
            self.llm = get_llm(llm_config)
        except Exception as e:
            yield json.dumps({"event": "error", "error": f"LLM Setup Error: {str(e)}"}) + "\n"
            return

        try:
            db = get_db()
            # NEVER db.engine.dialect.name: clickhouse-connect reports 'clickhousedb' and
            # Postgres reports 'postgresql', matching neither the registry nor sqlglot.
            spec = db.active_spec
            if spec is None:
                raise ConnectionError("No active database connection selected")
        except Exception as e:
            yield json.dumps({"event": "error", "error": f"Database offline: {str(e)}"}) + "\n"
            return

        yield json.dumps({"event": "agent_message", "agent": "Architect", "message": "Reading schemas and generating optimized SQL query..."}) + "\n"

        if catalog is None:
            try:
                # SchemaCache (repl/catalog.py) is the sole owner of metadata CACHING; the agent
                # never caches. It only fetches when no caller handed it a catalog, which is the
                # one-shot CLI path where there is no session to have warmed one.
                catalog = db.get_schema_metadata()
            except Exception as e:
                yield json.dumps({"event": "error", "error": f"Schema retrieval failed: {str(e)}"}) + "\n"
                return

        if not catalog.get("tables"):
            yield json.dumps({"event": "final_output", "text": "No tables detected. Verify your database connection."}) + "\n"
            return

        selection = select_relevant_tables(query, catalog, pinned_tables)
        yield json.dumps({
            "event": "schema_selection",
            "tables": selection["tables"],
            "scores": selection["scores"],
            "reasons": selection.get("reasons", {}),
            "strategy": selection.get("strategy", ""),
        }) + "\n"

        samples: Dict[str, Dict[str, Any]] = {}
        if bool(db.active_config.get("include_samples")):
            target = llm_target_label(llm_config)
            warning_key = (db.active_id, target)
            if warning_key not in _sample_warnings_emitted:
                _sample_warnings_emitted.add(warning_key)
                yield json.dumps({
                    "event": "agent_message",
                    "agent": "Sentry",
                    "message": (
                        f"⚠️ 'include_samples' is enabled for this connection: up to "
                        f"{SAMPLE_ROW_LIMIT} real rows from each selected table will be sent to "
                        f"{target}. Disable it in the connection profile to stop sending data."
                    ),
                }) + "\n"
            samples, _ = self.collect_samples(db, spec, selection["tables"], catalog)

        schemas = render_schema(catalog, selection["tables"], samples)

        sql_query = ""
        result = None
        previous_error = None
        attempts = 0
        max_attempts = 3

        while attempts < max_attempts:
            attempts += 1

            sql_query = await self.generate_sql(query, schemas, spec, previous_error)

            yield json.dumps({
                "event": "agent_message",
                "agent": "Programmer",
                "message": f"Generated SQL Query (Attempt {attempts}):\n```sql\n{sql_query}\n```"
            }) + "\n"

            yield json.dumps({"event": "agent_message", "agent": "Sentry", "message": "Auditing SQL syntax and query safety..."}) + "\n"
            # engine= makes the per-engine policy apply (SHOW on Trino, no s3()/url() on
            # ClickHouse, ...); allow_mutation is hard-coded False because the v1 pipeline is
            # read-only -- there is no approval UI, so nothing could approve a mutation.
            is_safe, security_msg = validate_sql_query(
                sql_query, dialect=spec.sqlglot_dialect, allow_mutation=False, engine=spec.name
            )
            if not is_safe:
                yield json.dumps({"event": "final_output", "text": f"Blocked Query Execution: {security_msg}"}) + "\n"
                return

            yield json.dumps({"event": "agent_message", "agent": "Executor", "message": "Executing SQL query..."}) + "\n"
            try:
                result = db.execute_query(sql_query)
                break
            except Exception as e:
                previous_error = str(e)
                yield json.dumps({
                    "event": "agent_message",
                    "agent": "Executor",
                    "message": f"⚠️ SQL execution failed: {previous_error}"
                }) + "\n"

                if attempts >= max_attempts:
                    yield json.dumps({"event": "error", "error": "Query failed after multiple self-correction repair attempts."}) + "\n"
                    return

        yield json.dumps({"event": "agent_message", "agent": "Analyst", "message": "Formatting database records..."}) + "\n"
        analysis = await self.analyze_and_format(query, sql_query, result)

        yield json.dumps({
            "event": "swarm_completed",
            "sql": sql_query,
            "summary": analysis.get("summary", ""),
            "raw_data": result,
        }) + "\n"
