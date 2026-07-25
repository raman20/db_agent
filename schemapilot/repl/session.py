"""``ReplSession``: everything the interactive shell knows.

State ownership is fixed by the cross-task contract:

* ``session.db``             -- :class:`~schemapilot.db.DatabaseManager`, execution + connection.
* ``session.catalog``        -- :class:`~schemapilot.catalog.SchemaCache`, the only schema owner.
* ``session.pinned_tables``  -- set by ``@table`` completion, cleared after each question.
* ``session.last_selection`` -- the agent's ``schema_selection`` payload, reported by ``/why``.

The session is deliberately constructible without a terminal (``console``/``prompt`` are only
needed by :meth:`run`), because the command dispatch tests drive handlers directly.
"""

import asyncio
import inspect
import json
import logging
import os
import traceback
from typing import Any, Callable, Dict, List, Optional

from rich.console import Console
from rich.markup import escape

from schemapilot.catalog import SchemaCache
from schemapilot.config import settings
from schemapilot.db import get_db
from schemapilot.llm import get_model_manager

logger = logging.getLogger("schemapilot.repl")

USER_CONFIG_DIR = os.path.expanduser("~/.config/schemapilot")
LOG_FILE = os.path.join(USER_CONFIG_DIR, "schemapilot.log")


class ReplSession:
    """Mutable state plus the small amount of behaviour the command handlers share."""

    def __init__(
        self,
        db=None,
        console: Optional[Console] = None,
        model_manager=None,
        llm_config: Optional[Dict[str, Any]] = None,
        event_renderer: Optional[Callable[[Dict[str, Any]], None]] = None,
        log_file: str = LOG_FILE,
    ):
        self.db = db if db is not None else get_db()
        self.console = console or Console()
        self.model_manager = model_manager if model_manager is not None else get_model_manager()
        self.llm_config = llm_config or {}
        self.catalog = SchemaCache(self.db, max_tables=settings.CATALOG_MAX_TABLES)

        #: Tables the user pinned with ``@name``; consumed by the next question, then cleared.
        self.pinned_tables: List[str] = []
        #: The agent's last table-pruning decision, or None until T6's event arrives.
        self.last_selection: Optional[Dict[str, Any]] = None

        self.history: List[Dict[str, str]] = []
        self.running = True

        self.event_renderer = event_renderer or self._default_event_renderer
        self.log_file = log_file
        #: The log-file location is mentioned exactly once per session, not on every error.
        self._log_hint_shown = False

    # ------------------------------------------------------------------ status / header

    @property
    def spec(self):
        """The active :class:`EngineSpec`, or None. Never read ``db.engine.dialect.name``:
        clickhouse-connect reports ``clickhousedb`` and Postgres ``postgresql``, and neither
        matches our registry keys or sqlglot's dialect names."""
        return self.db.active_spec

    @property
    def connection_label(self) -> str:
        config = self.db.active_config
        if not config:
            return "no connection"
        name = config.get("name") or self.db.active_id
        spec = self.spec
        return f"{name} · {spec.name if spec else '?'}"

    @property
    def model_label(self) -> str:
        profile = self.model_manager.get_active_profile()
        return str(profile.get("model_name") or "unconfigured")

    def status_line(self) -> str:
        """Prompt toolbar text. ``ro`` is not a mode indicator with an alternative: the v1 REPL
        is read-only, full stop (no ``/safe-mode``, no per-connection ``rw``)."""
        return f"[{self.connection_label} · ro] [{self.model_label}] [{self.catalog.summary()}]"

    # ------------------------------------------------------------------ errors

    def fail(self, headline: str, action: str = "", error: Optional[BaseException] = None):
        """Reports a failure as ONE headline plus ONE action, never a stack trace.

        A traceback at the prompt scrolls the useful line off the screen and tells the user
        nothing they can act on; it goes to the log file instead, whose path is mentioned once.
        """
        # escape(): headlines carry database/driver error text, which routinely contains square
        # brackets that Rich would otherwise eat as a style tag.
        self.console.print(f"[bold red]✗ {escape(str(headline))}[/bold red]")
        if action:
            self.console.print(f"  [yellow]→[/yellow] {escape(str(action))}")
        if error is not None:
            self._log_traceback(headline, error)

    def _log_traceback(self, headline: str, error: BaseException):
        try:
            os.makedirs(os.path.dirname(self.log_file), exist_ok=True)
            with open(self.log_file, "a") as handle:
                handle.write(f"\n--- {headline} ---\n")
                handle.write("".join(traceback.format_exception(type(error), error, error.__traceback__)))
        except OSError as exc:  # pragma: no cover - unwritable HOME
            logger.debug(f"Could not write {self.log_file}: {exc}")
            return
        if not self._log_hint_shown:
            self.console.print(f"  [dim]Details logged to {self.log_file}[/dim]")
            self._log_hint_shown = True

    def describe_error(self, error: BaseException) -> Dict[str, str]:
        """Turns an exception into a headline + the single action that fixes it.

        The four cases below are the ones users actually hit; everything else falls through to a
        generic message rather than a guess that sends them down the wrong path.
        """
        text = str(error)
        lowered = text.lower()
        spec = self.spec

        # 1. Missing optional driver -- the exact pip command, from the spec itself.
        if "driver is not installed" in lowered or "nosuchmodule" in type(error).__name__.lower():
            hint = spec.install_hint() if spec else ""
            return {
                "headline": text if "driver" in lowered else f"Database driver missing: {text}",
                "action": hint or "Install the engine's Python driver and retry.",
            }

        # 2. Authentication -- name the field and where it is stored, so the fix is one edit.
        if any(token in lowered for token in ("authentication", "access denied", "password", "auth failed", "401 unauthorized")):
            if "api" in lowered or "unauthorized" in lowered:
                return self._llm_auth_error(text)
            return {
                "headline": "Database authentication failed",
                "action": (
                    "Check the 'username' / 'password' fields of this profile in "
                    "~/.config/schemapilot/connections.json, or re-create it with --add-conn."
                ),
            }

        # 3. LLM credentials -- which env var was read, and the flag that persists a key.
        if "api key" in lowered or "api_key" in lowered:
            return self._llm_auth_error(text)

        # 4. Nothing connected.
        if "no active database connection" in lowered:
            return {
                "headline": "No active database connection",
                "action": "Run `schemapilot --list-conns` to see profiles, then `/use <name>` here.",
            }

        return {"headline": text or type(error).__name__, "action": ""}

    def _llm_auth_error(self, text: str) -> Dict[str, str]:
        """Names the env var actually consulted for the active provider."""
        provider = str(self.model_manager.get_active_profile().get("provider") or "google").lower()
        env_var = {
            "google": "GOOGLE_API_KEY",
            "openai": "OPENAI_API_KEY",
            "anthropic": "ANTHROPIC_API_KEY",
        }.get(provider, "LLM_API_KEY")
        return {
            "headline": f"LLM authentication failed for provider '{provider}'",
            "action": (
                f"SchemaPilot read {env_var} from the environment/.env; set it, or store a key "
                "with `schemapilot --add-model`."
            ),
        }

    def report(self, error: BaseException):
        """Convenience: describe an exception and print it in the one-headline-one-action shape."""
        described = self.describe_error(error)
        self.fail(described["headline"], described["action"], error=error)

    # ------------------------------------------------------------------ connections

    def resolve_connection(self, name: str) -> Optional[str]:
        """Maps a user-typed token to a connection id, accepting the id or the friendly name."""
        if not name:
            return None
        needle = name.strip().lower()
        connections = self.db.connections
        if name in connections:
            return name
        for conn_id, config in connections.items():
            if conn_id.lower() == needle or str(config.get("name", "")).lower() == needle:
                return conn_id
        # Last resort: a unique id prefix, so a UUID profile is usable without pasting it whole.
        prefixed = [cid for cid in connections if cid.lower().startswith(needle)]
        return prefixed[0] if len(prefixed) == 1 else None

    def connection_choices(self) -> List[str]:
        """Completion values for ``/use``: friendly names where present, ids otherwise."""
        choices = []
        for conn_id, config in self.db.connections.items():
            name = config.get("name")
            choices.append(name if name else conn_id)
        return sorted(set(choices))

    def switch_connection(self, conn_id: str) -> bool:
        """Activates a connection and warms the catalog SYNCHRONOUSLY.

        Synchronous on purpose. Warming in a background thread would race this very method:
        ``select_connection`` swaps ``db.engine`` / ``db.active_id`` / ``db.active_spec`` under
        the warm job, which would then file the previous database's tables under the new
        connection id. A visible spinner is a cheaper answer than locking the engine swap.
        """
        self.catalog.invalidate()
        self.last_selection = None
        self.pinned_tables = []

        if not self.db.select_connection(conn_id):
            return False

        with self.console.status(f"[bold blue]Reading schema for {self.connection_label}..."):
            try:
                self.catalog.warm(force=True)
            except Exception as exc:
                # A connected-but-unreadable database is still usable for /sql, so this is a
                # warning rather than a failed switch.
                described = self.describe_error(exc)
                self.fail(f"Connected, but schema introspection failed: {described['headline']}",
                          described["action"], error=exc)
        return True

    # ------------------------------------------------------------------ pinning

    def pin_table(self, name: str):
        """Records a table pinned by ``@name`` completion (deduplicated, order preserved)."""
        resolved = self.catalog.resolve(name) or name
        if resolved not in self.pinned_tables:
            self.pinned_tables.append(resolved)

    # ------------------------------------------------------------------ questions

    def ask(self, question: str):
        """Runs a natural-language question through the agent swarm."""
        if not self.db.active_id:
            self.fail(
                "No active database connection",
                "Run `schemapilot --list-conns` to see profiles, then `/use <name>` here.",
            )
            return

        pinned = list(self.pinned_tables)
        try:
            asyncio.run(self._run_agent(question, pinned))
        except Exception as exc:
            self.report(exc)
        finally:
            # Pins are per-question by design: a table pinned for "top customers" should not
            # silently steer the next, unrelated question.
            self.pinned_tables = []

    async def _run_agent(self, question: str, pinned: List[str]):
        # Imported here rather than at module import time so the REPL package stays importable
        # (and unit-testable) without pulling in langchain and the whole LLM stack.
        from schemapilot.agent import SchemaPilotAgent

        agent = SchemaPilotAgent()
        self.history.append({"role": "user", "content": question})

        with self.console.status("[bold blue]Resolving database schema...") as status:
            # Inside the status block because building the kwargs can include warming the
            # catalog, which is the slow part on a warehouse and should show the spinner.
            kwargs = self._agent_kwargs(agent, pinned)
            async for chunk in agent.execute(question, self.history[:-1], self.llm_config, **kwargs):
                if not chunk.strip():
                    continue
                try:
                    event = json.loads(chunk.strip())
                except json.JSONDecodeError:
                    continue

                if event.get("event") == "schema_selection":
                    # Stored, not printed: /why is where the user asks for it.
                    self.last_selection = {
                        "tables": event.get("tables") or [],
                        "scores": event.get("scores") or {},
                    }
                elif event.get("event") == "agent_message":
                    name = event.get("agent", "Agent")
                    message = str(event.get("message", "")).split("\n")[0]
                    status.update(f"[bold blue][{name}] {message}...")
                else:
                    self.event_renderer(event)

    def _agent_kwargs(self, agent, pinned: List[str]) -> Dict[str, Any]:
        """Builds the optional keyword arguments for ``agent.execute``.

        ``catalog=`` is the important one: the session hands the agent its ALREADY WARMED
        metadata, so the agent introspects nothing. Without it the agent re-ran
        ``get_schema_metadata()`` on every single question -- duplicating slow warehouse
        introspection that ``/use`` had just done, and, worse, letting completion, ``/schema``,
        ``@`` pin resolution and the LLM prompt each observe a different catalog. ``SchemaCache``
        is the sole owner of this metadata, so it is the only thing that should ever fetch it.

        The value handed over is the raw ``get_schema_metadata()`` dict verbatim
        (``{"tables": ..., "relationships": ..., "truncated": ..., "total_tables": ...}``), not a
        ``SchemaCache``: the agent must stay usable by callers that have no session.
        """
        kwargs: Dict[str, Any] = {}

        if pinned and self._accepts(agent, "pinned_tables"):
            kwargs["pinned_tables"] = pinned

        if self._accepts(agent, "catalog"):
            try:
                # warm() is a no-op when /use already warmed it; on the first question of a
                # cold session it does the one fetch this session will ever need.
                kwargs["catalog"] = self.catalog.warm()
            except Exception as exc:
                # Omitting the kwarg falls back to the agent's own introspection, which will
                # report the failure through its error event. Handing over a half-built or empty
                # catalog would be worse: the agent trusts a supplied catalog absolutely and
                # would answer "No tables detected" for what is really a transient read failure.
                logger.warning(f"Could not warm the catalog for this question: {exc}")

        return kwargs

    @staticmethod
    def _accepts(agent, parameter: str) -> bool:
        """Whether this agent build takes ``parameter=``.

        Checked rather than assumed so the REPL works against agent builds both with and without
        the pruning/catalog parameters, instead of dying with a TypeError on the first question.
        """
        try:
            return parameter in inspect.signature(agent.execute).parameters
        except (TypeError, ValueError):  # pragma: no cover - builtins/C callables
            return False

    # ------------------------------------------------------------------ default rendering

    def _default_event_renderer(self, event: Dict[str, Any]):
        """Minimal fallback renderer.

        ``cli.py`` injects its own Rich renderer; this exists so a ``ReplSession`` built in
        isolation (tests, embedding) still shows something instead of swallowing events.
        """
        kind = event.get("event")
        if kind == "error":
            self.fail(str(event.get("error", "unknown error")))
        elif kind == "final_output":
            self.console.print(str(event.get("text", "")))
        elif kind == "swarm_completed":
            self.console.print(str(event.get("summary", "")))

    # ------------------------------------------------------------------ the loop

    def run(self):
        """The interactive loop. Needs a terminal; everything else in this class does not."""
        from prompt_toolkit import PromptSession
        from prompt_toolkit.formatted_text import HTML
        from prompt_toolkit.history import FileHistory

        from schemapilot.repl.commands import handle_line, print_banner

        os.makedirs(USER_CONFIG_DIR, exist_ok=True)
        prompt_session = PromptSession(
            history=FileHistory(os.path.join(USER_CONFIG_DIR, ".repl_history")),
            completer=self._build_completer(),
            complete_while_typing=True,
        )

        # Warm on entry so completion is useful from the first keypress -- the completer itself
        # performs no I/O and would otherwise stay silent until the first /tables.
        if self.db.active_id and not self.catalog.is_warm:
            with self.console.status("[bold blue]Reading schema..."):
                try:
                    self.catalog.warm()
                except Exception as exc:
                    self.report(exc)

        print_banner(self)

        while self.running:
            try:
                line = prompt_session.prompt(
                    HTML("<cyan><b>schemapilot &gt; </b></cyan>"),
                    bottom_toolbar=self.status_line(),
                ).strip()
            except KeyboardInterrupt:
                # ^C cancels the current line, matching the toolbar's promise.
                continue
            except EOFError:
                break

            if not line:
                continue
            try:
                handle_line(self, line)
            except Exception as exc:
                self.report(exc)

        self.console.print("Goodbye.")

    def _build_completer(self):
        from schemapilot.repl.completion import SchemaPilotCompleter

        return SchemaPilotCompleter(self)
