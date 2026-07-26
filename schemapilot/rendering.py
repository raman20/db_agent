"""One renderer for query result rows, shared by the REPL and the one-shot CLI.

Why this module exists: there were two row-table renderers -- ``cli.py``'s (for the
``swarm_completed`` event on the ``schemapilot "question"`` path) and ``repl/commands.py``'s (for
``/sql``) -- and they disagreed on both things a renderer decides. They truncated at different
row counts, so the same result printed differently depending on which surface you were on, and
only one of them escaped Rich markup.

**The escaping is not cosmetic.** Rich reads square brackets as style tags and *deletes* the ones
it does not recognise::

    Console.print('pkg schemapilot[trino] and [glob]')   ->   'pkg schemapilot and '

Result cells are arbitrary database content: a JSON column, a Postgres array literal
(``{1,2}``/``[1,2]``), a ``DECIMAL(10,2)[]`` type name, a product called ``[legacy]``. Unescaped,
Rich silently drops that text and the user reads a *wrong value* with nothing to indicate it. So
every header and every cell goes through ``rich.markup.escape`` here, once, for both callers.
"""

from typing import Any, Dict, List, Optional, Sequence

from rich.markup import escape
from rich.table import Table

#: Rows printed before the preview is truncated.
#:
#: 25 rather than the 15 ``cli.py`` used: 25 is roughly one screenful on a standard terminal, so
#: the common "eyeball the result" case needs no scrollback, while the truncation notice still
#: tells the user the full row count. The number is deliberately one constant for both surfaces --
#: a preview that changes length depending on whether you asked in the REPL or on the command line
#: is a difference with no meaning behind it.
PREVIEW_ROWS = 25


def build_result_table(columns: Sequence[Any], rows: Sequence[Dict[str, Any]],
                       max_rows: int = PREVIEW_ROWS) -> Table:
    """A Rich table of ``rows``, capped at ``max_rows``, with every cell markup-escaped."""
    table = Table(show_header=True, header_style="bold cyan", border_style="dim")
    for column in columns:
        table.add_column(escape(str(column)))
    for row in list(rows)[:max_rows]:
        table.add_row(*[
            "" if row.get(column) is None else escape(str(row.get(column)))
            for column in columns
        ])
    return table


def print_result_rows(console, result: Optional[Dict[str, Any]],
                      max_rows: int = PREVIEW_ROWS) -> bool:
    """Prints an ``execute_query`` payload (``{"columns": [...], "rows": [...]}``).

    Returns True when a table was printed. A payload with no ``rows`` key is not a result set
    (drivers return ``{"message": ...}`` for statements that yield none), so the caller is left to
    decide what to say about it.
    """
    if not isinstance(result, dict) or "rows" not in result:
        return False

    columns: List[Any] = list(result.get("columns") or [])
    rows: List[Dict[str, Any]] = list(result.get("rows") or [])
    if not rows:
        console.print("[dim]0 rows.[/dim]")
        return True

    console.print(build_result_table(columns, rows, max_rows=max_rows))
    if len(rows) > max_rows:
        console.print(f"[dim]Showing {max_rows} of {len(rows)} rows.[/dim]")
    return True
