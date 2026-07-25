"""prompt_toolkit completion for the REPL.

Two rules govern this module.

**1. The completer performs NO I/O.** ``get_completions`` runs on the keypress path, so a call
into ``get_schema_metadata()`` here would freeze the terminal mid-word against a slow warehouse
(Trino's coordinator can take seconds to answer). It therefore reads only what
:class:`~schemapilot.catalog.SchemaCache` already holds and yields **nothing** when the cache is
cold -- no suggestion is a better failure than a stalled keystroke. The cache is warmed on entry
and on ``/use``, synchronously, under a visible Rich status.

**2. ``@table`` pins.** Completion of a bare-text ``@name`` also records the table in
``session.pinned_tables``, which is what makes completion buy *answer accuracy* rather than
typing speed: the pinned table is force-included in the prompt the agent builds.
"""

from typing import Iterable, List

from prompt_toolkit.completion import Completer, Completion

from schemapilot.names import completion_candidates
from schemapilot.repl.commands import COMMANDS, COMMAND_ORDER


class SchemaPilotCompleter(Completer):
    """Completes command names, per-command arguments, and ``@table`` references."""

    def __init__(self, session):
        self.session = session

    # ------------------------------------------------------------------ entry point

    def get_completions(self, document, complete_event) -> Iterable[Completion]:
        text = document.text_before_cursor
        stripped = text.lstrip()

        if stripped.startswith("/"):
            body = stripped[1:]
            if " " not in body:
                yield from self._command_completions(body)
            else:
                name, _, args = body.partition(" ")
                yield from self._argument_completions(name, args)
            return

        # Free text: the only thing worth completing is an @table reference.
        yield from self._at_completions(text)

    # ------------------------------------------------------------------ commands

    def _command_completions(self, prefix: str) -> Iterable[Completion]:
        lowered = prefix.lower()
        for name in COMMAND_ORDER:
            if name.startswith(lowered):
                yield Completion(
                    name,
                    start_position=-len(prefix),
                    display=f"/{name}",
                    display_meta=COMMANDS[name].summary,
                )

    def _argument_completions(self, name: str, args: str) -> Iterable[Completion]:
        command = COMMANDS.get(name.strip().lower())
        if command is None or not command.arg_source:
            return

        # Complete only the token under the cursor, not the whole argument string.
        prefix = args.rsplit(" ", 1)[-1] if args else ""
        lowered = prefix.lower()

        for value in self._values_for(command.arg_source):
            if lowered in value.lower():
                yield Completion(value, start_position=-len(prefix))

    def _values_for(self, source: str) -> List[str]:
        """Candidate argument values. Reads cached state only -- never introspects."""
        if source == "connections":
            return self.session.connection_choices()
        if source == "tables":
            # Empty while cold: see rule 1 in the module docstring.
            return self.session.catalog.table_names()
        if source == "commands":
            return list(COMMAND_ORDER)
        return []

    # ------------------------------------------------------------------ @table pinning

    def _at_completions(self, text: str) -> Iterable[Completion]:
        at_index = text.rfind("@")
        if at_index == -1:
            return

        prefix = text[at_index + 1:]
        if " " in prefix:
            # The @token is already finished and the user has moved on.
            return

        candidates = self._table_candidates(prefix)
        if not candidates:
            return

        if len(candidates) == 1:
            # prompt_toolkit has no "completion accepted" callback, so pin the unambiguous
            # candidate now: it is the one the user is about to accept, and pins are cleared
            # after every question, so an over-eager pin costs at most one extra table in one
            # prompt -- much cheaper than silently dropping the table the user asked for.
            self.session.pin_table(candidates[0])

        for name in candidates:
            yield Completion(name, start_position=-len(prefix), display_meta="pin this table")

    def _table_candidates(self, prefix: str) -> List[str]:
        """Qualified names whose full or bare name starts with ``prefix`` (case-insensitive).

        Delegates to :mod:`schemapilot.names` so completion normalises names exactly the way the
        cache and pruning resolve them -- otherwise a name can complete here and then resolve to a
        different table, or to none, further down.
        """
        return completion_candidates(prefix, self.session.catalog.table_names())
