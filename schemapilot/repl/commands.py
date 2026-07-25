"""The command dispatch TABLE and the eight v1 commands.

Dispatch is a plain dict (``name -> Command``) rather than an ``if/elif`` ladder or method
name-mangling so routing is data that can be asserted directly, without driving a terminal.

**Exactly eight commands in v1** -- ``/help /use /engines /tables /schema /sql /why /exit``.
``/connect``, ``/conns``, ``/model`` and ``/rels`` are deliberately absent: the argparse flags
(``--add-conn``, ``--list-conns``, ``--select-conn``, ``--add-model``, ``--select-model``)
already cover connection and model management, and ``/schema`` already renders FK arrows, so all
four would be duplicate surface. Anything not starting with ``/`` is a natural-language question.

**The v1 REPL is strictly read-only.** ``/sql`` always calls the sentry with
``allow_mutation=False``; there is no ``/safe-mode``, no per-connection ``rw``, and
``settings.ALLOW_MUTATING_QUERIES`` deliberately does not unlock it -- mutation ships later
together with the interactive approval panel that would make it reviewable.
"""

import difflib
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

from rich.markup import escape
from rich.table import Table
from rich.tree import Tree

from schemapilot.engines import ENGINES, driver_available
from schemapilot.security import validate_sql_query

#: How many rows of a /sql result to print before truncating the preview.
SQL_PREVIEW_ROWS = 25


@dataclass(frozen=True)
class Command:
    """One REPL command: its handler, its one-line help, and its usage string."""

    name: str
    handler: Callable[["object", str], None]
    summary: str
    usage: str
    #: Name of the argument completer to use, resolved by completion.py. None = no arguments.
    arg_source: Optional[str] = None


# --------------------------------------------------------------------------- helpers


def _require_connection(session) -> bool:
    """Prints the one-action fix and returns False when nothing is connected."""
    if session.db.active_id:
        return True
    session.fail(
        "No active database connection",
        "Run `schemapilot --list-conns` to see profiles, then `/use <name>` here.",
    )
    return False


def _ensure_catalog(session) -> bool:
    """Warms the catalog for a command that needs it (commands may block; the completer cannot)."""
    if not _require_connection(session):
        return False
    if session.catalog.is_warm:
        return True
    with session.console.status("[bold blue]Reading schema..."):
        try:
            session.catalog.warm()
        except Exception as exc:
            session.report(exc)
            return False
    return True


def _column_label(column: Dict[str, object]) -> str:
    """``name: type`` plus PK/FK markers. No row counts -- nothing provides them, so nothing
    claims them."""
    # escape(): a column named `[legacy]` or a type like `DECIMAL(10,2)[]` would otherwise be
    # parsed as Rich markup and vanish from the output.
    label = f"[bold]{escape(str(column['name']))}[/bold]: [dim]{escape(str(column['type']))}[/dim]"
    if column.get("is_primary"):
        label += " [yellow](PK)[/yellow]"
    if column.get("foreign_key"):
        label += f" [cyan]→ {escape(str(column['foreign_key']))}[/cyan]"
    if not column.get("nullable", True):
        label += " [dim]NOT NULL[/dim]"
    return label


# --------------------------------------------------------------------------- handlers


def cmd_help(session, args: str):
    """`/help [cmd]` -- all commands, or the detail for one."""
    target = args.strip().lstrip("/")
    if target:
        command = COMMANDS.get(target)
        if not command:
            session.fail(f"Unknown command '/{target}'", _suggestion_action(target))
            return
        # escape(): usage strings contain optional-argument brackets (`/help [cmd]`) that Rich
        # would otherwise treat as a style tag and delete.
        session.console.print(f"[bold]{escape(command.usage)}[/bold]")
        session.console.print(f"  {escape(command.summary)}")
        return

    table = Table(show_header=True, header_style="bold blue", border_style="dim")
    table.add_column("Command")
    table.add_column("What it does")
    for name in COMMAND_ORDER:
        command = COMMANDS[name]
        table.add_row(escape(command.usage), escape(command.summary))
    session.console.print(table)
    session.console.print(
        "[dim]Bare text is a natural-language question. "
        "@table pins a table so the next question definitely sees it. "
        "This session is read-only.[/dim]"
    )


def cmd_use(session, args: str):
    """`/use <name>` -- switch connection and warm the catalog."""
    name = args.strip()
    if not name:
        session.fail("/use needs a connection", "Try `/use <name>`; `schemapilot --list-conns` lists them.")
        return

    conn_id = session.resolve_connection(name)
    if conn_id is None:
        session.fail(
            f"No connection profile matches '{name}'",
            "Run `schemapilot --list-conns` to see the available profile names and ids.",
        )
        return

    try:
        switched = session.switch_connection(conn_id)
    except Exception as exc:
        session.report(exc)
        return

    if not switched:
        session.fail(f"Could not activate '{name}'", "Run `schemapilot --list-conns` and retry with an exact id.")
        return

    session.console.print(
        f"[green]✓[/green] Now on [bold]{escape(session.connection_label)}[/bold] "
        f"[dim]({session.catalog.summary()})[/dim]"
    )


def cmd_engines(session, args: str):
    """`/engines` -- SchemaPilot's policy per engine, not the engine's platform capabilities."""
    active = session.spec.name if session.spec else None

    table = Table(show_header=True, header_style="bold blue", border_style="dim")
    table.add_column("")
    table.add_column("Engine")
    table.add_column("Driver")
    table.add_column("Safe dry-run")
    table.add_column("Cross-engine reach")
    table.add_column("Fix / note")

    for name in sorted(ENGINES):
        spec = ENGINES[name]
        available = driver_available(spec)
        driver = "[green]installed[/green]" if available else "[red]missing[/red]"
        # "safe dry-run: unavailable" rather than "transactions: no". Trino's DBAPI *does*
        # expose commit/rollback -- the honest claim is that SchemaPilot cannot guarantee a
        # rollback across arbitrary connectors, which is a statement about our guarantee.
        dry_run = "[green]available[/green]" if spec.supports_safe_dry_run else "[yellow]unavailable[/yellow]"
        reach = spec.cross_engine_via or "[dim]—[/dim]"
        # escape(): install_hint() contains `schemapilot[trino]`, and Rich would otherwise read
        # the square brackets as a style tag and silently delete the extra name -- turning the
        # exact fix into a command that installs the wrong thing.
        note = "" if available else escape(spec.install_hint() or "no optional extra; check the driver install")
        table.add_row(
            "[bold green]●[/bold green]" if name == active else "",
            spec.display_name,
            driver,
            dry_run,
            reach,
            note,
        )

    session.console.print(table)
    session.console.print(
        "[dim]'Safe dry-run' is SchemaPilot's own guarantee, not the engine's transaction "
        "support. This session is read-only regardless.[/dim]"
    )


def cmd_tables(session, args: str):
    """`/tables [glob]` -- qualified table names, optionally filtered."""
    if not _ensure_catalog(session):
        return

    pattern = args.strip()
    names = session.catalog.match(pattern)
    if not names:
        if pattern:
            session.fail(f"No table matches '{pattern}'", "Run `/tables` with no filter to see everything cached.")
        else:
            session.fail("No tables in the cached catalog", "Check the connection's schema/database, then `/use` it again.")
        return

    for name in names:
        session.console.print(f"  {escape(name)}")
    suffix = f" matching '{pattern}'" if pattern else ""
    session.console.print(f"[dim]{len(names)} table(s){suffix} · {session.catalog.summary()}[/dim]")


def cmd_schema(session, args: str):
    """`/schema [table]` -- Rich Tree of schemas → tables → columns with PK/FK markers."""
    if not _ensure_catalog(session):
        return

    target = args.strip()
    if target:
        resolved = session.catalog.resolve(target)
        if resolved is None:
            session.fail(f"Table '{target}' is not in the cached catalog", "Run `/tables` to see the cached names.")
            return
        tree = Tree(f"[bold cyan]{escape(resolved)}[/bold cyan]")
        for column in session.catalog.columns(resolved):
            tree.add(_column_label(column))
        session.console.print(tree)
        return

    tree = Tree(f"[bold]{escape(session.connection_label)}[/bold]")
    for schema, names in sorted(session.catalog.tables_by_schema().items()):
        # Single-part engines (SQLite) have no schema: hang their tables off the root rather
        # than inventing an empty schema node.
        parent = tree if not schema else tree.add(f"[bold blue]{escape(schema)}[/bold blue]")
        for qualified in names:
            info = session.catalog.table(qualified)
            leaf = parent.add(f"[cyan]{escape(str((info or {}).get('table') or qualified))}[/cyan]")
            for column in session.catalog.columns(qualified):
                leaf.add(_column_label(column))

    session.console.print(tree)
    if session.catalog.truncated:
        session.console.print(
            f"[yellow]Showing {len(session.catalog.table_names())} of "
            f"{session.catalog.total_tables} tables (CATALOG_MAX_TABLES).[/yellow]"
        )


def cmd_sql(session, args: str):
    """`/sql <raw>` -- run SQL with no LLM, but still through the sentry."""
    sql = args.strip()
    if not sql:
        session.fail("/sql needs a statement", "Try `/sql SELECT 1`.")
        return
    if not _require_connection(session):
        return

    spec = session.spec
    # allow_mutation is hard-coded False: the v1 REPL is read-only (see the module docstring),
    # and settings.ALLOW_MUTATING_QUERIES deliberately does not reach this call.
    is_safe, message = validate_sql_query(
        sql,
        dialect=spec.sqlglot_dialect,
        allow_mutation=False,
        engine=spec.name,
    )
    if not is_safe:
        session.fail(message, "This session is read-only; only read queries run here.")
        return

    try:
        result = session.db.execute_query(sql)
    except Exception as exc:
        session.report(exc)
        return

    _render_result(session, result)


def _render_result(session, result: Dict[str, object]):
    """Prints an ``execute_query`` payload: ``{"columns": [...], "rows": [...]}`` or a message."""
    if not isinstance(result, dict) or "rows" not in result:
        session.console.print(str((result or {}).get("message", result)))
        return

    columns: List[str] = list(result.get("columns") or [])
    rows: List[Dict[str, object]] = list(result.get("rows") or [])
    if not rows:
        session.console.print("[dim]0 rows.[/dim]")
        return

    table = Table(show_header=True, header_style="bold cyan", border_style="dim")
    for column in columns:
        table.add_column(escape(str(column)))
    for row in rows[:SQL_PREVIEW_ROWS]:
        # escape(): result cells are arbitrary database content and must never be interpreted
        # as Rich markup.
        table.add_row(*["" if row.get(c) is None else escape(str(row.get(c))) for c in columns])

    session.console.print(table)
    if len(rows) > SQL_PREVIEW_ROWS:
        session.console.print(f"[dim]Showing {SQL_PREVIEW_ROWS} of {len(rows)} rows.[/dim]")


def cmd_why(session, args: str):
    """`/why` -- report which tables the last question sent to the model, and why."""
    selection = session.last_selection
    if not selection or not selection.get("tables"):
        # Honest rather than crashing: table pruning is T6's, and until its schema_selection
        # event arrives there is genuinely nothing to explain.
        session.console.print(
            "[yellow]No table-selection decision recorded.[/yellow]\n"
            "  [dim]Ask a question first. If this persists, table pruning is not yet enabled "
            "in this build and the full catalog is sent.[/dim]"
        )
        return

    scores = selection.get("scores") or {}
    table = Table(show_header=True, header_style="bold blue", border_style="dim")
    table.add_column("Table sent to the model")
    table.add_column("Relevance score", justify="right")
    for name in selection["tables"]:
        score = scores.get(name)
        table.add_row(escape(str(name)), "—" if score is None else f"{float(score):.2f}")
    session.console.print(table)
    if session.pinned_tables:
        session.console.print(f"[dim]Pinned for the next question: {', '.join(session.pinned_tables)}[/dim]")


def cmd_exit(session, args: str):
    """`/exit` -- leave the shell."""
    session.running = False


# --------------------------------------------------------------------------- the table

#: The dispatch table. Routing is data, so a test can assert it without a terminal.
COMMANDS: Dict[str, Command] = {
    "help": Command("help", cmd_help, "Show commands, or detail for one.", "/help [cmd]", "commands"),
    "use": Command("use", cmd_use, "Switch connection and warm the schema cache.", "/use <name>", "connections"),
    "engines": Command("engines", cmd_engines, "Driver status and SchemaPilot's policy per engine.", "/engines"),
    "tables": Command("tables", cmd_tables, "List cached tables, optionally glob-filtered.", "/tables [glob]", "tables"),
    "schema": Command("schema", cmd_schema, "Tree of schemas, tables and columns (PK/FK marked).", "/schema [table]", "tables"),
    "sql": Command("sql", cmd_sql, "Run raw SQL with no LLM (still sentry-checked, read-only).", "/sql <raw>"),
    "why": Command("why", cmd_why, "Explain which tables the last question used.", "/why"),
    "exit": Command("exit", cmd_exit, "Leave SchemaPilot.", "/exit"),
}

#: Presentation order for ``/help`` (dict order is insertion order, but stated explicitly so a
#: future reordering of COMMANDS cannot silently reshuffle the help output).
COMMAND_ORDER = ("help", "use", "engines", "tables", "schema", "sql", "why", "exit")


def suggest(name: str) -> Optional[str]:
    """The nearest command to a typo, or None."""
    matches = difflib.get_close_matches(name.strip().lstrip("/").lower(), list(COMMANDS), n=1, cutoff=0.5)
    return matches[0] if matches else None


def _suggestion_action(name: str) -> str:
    nearest = suggest(name)
    return f"Did you mean /{nearest}?" if nearest else "Run /help to see the eight available commands."


def dispatch(session, name: str, args: str = "") -> bool:
    """Routes one command name to its handler. Returns False when the name is unknown."""
    command = COMMANDS.get(name.strip().lstrip("/").lower())
    if command is None:
        return False
    command.handler(session, args)
    return True


def handle_line(session, line: str):
    """Handles one line of input: a slash command, or a natural-language question."""
    text = line.strip()
    if not text:
        return

    if not text.startswith("/"):
        session.ask(text)
        return

    name, _, args = text[1:].partition(" ")
    if not dispatch(session, name, args.strip()):
        session.fail(f"Unknown command '/{name}'", _suggestion_action(name))


def print_banner(session):
    """The header shown once on entry: connection, model, catalog size, read-only state."""
    from rich.panel import Panel

    session.console.print(
        Panel(
            f"[bold]conn[/bold]  {escape(session.connection_label)}   "
            f"[yellow]{escape('[read-only]')}[/yellow]\n"
            f"[bold]model[/bold] {escape(session.model_label)}   "
            f"[dim]{escape(session.catalog.summary())}[/dim]",
            title="SchemaPilot",
            border_style="blue",
            expand=False,
        )
    )
    session.console.print("[dim]Type a question, or /help. @table pins a table into the prompt.[/dim]")
