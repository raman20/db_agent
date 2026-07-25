"""The interactive REPL: session state, the command dispatch table, and completion.

Split out of ``cli.py`` so the shell has one owner. ``cli.py`` keeps argparse (which already
covers connection and model management) and delegates the interactive body here.
"""

from schemapilot.repl.commands import COMMANDS, dispatch, handle_line, suggest
from schemapilot.repl.completion import SchemaPilotCompleter
from schemapilot.repl.session import ReplSession

__all__ = [
    "ReplSession",
    "SchemaPilotCompleter",
    "COMMANDS",
    "dispatch",
    "handle_line",
    "suggest",
]
