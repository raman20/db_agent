# Contributing to SchemaPilot

SchemaPilot is a local-only terminal CLI that translates natural language to SQL and runs it against your database. It supports six engines, has a per-engine security sentry, and an interactive REPL.

## Setup

```bash
git clone https://github.com/raman20/SchemaPilot.git
cd SchemaPilot
pip install -e ".[all,dev]"
cp .env.example .env      # fill in your LLM provider details
docker compose up -d       # starts postgres, mysql, clickhouse, trino
```

## Testing

```bash
# Unit tests (no Docker, no API keys)
make test

# Live six-engine integration matrix (needs docker compose up -d first)
make test-integration
```

No test makes a real LLM call — the suite uses SQLite and DuckDB in-process for unit tests, and the Docker sandbox for Postgres/MySQL/ClickHouse/Trino integration tests. Set `SCHEMAPILOT_IT=1` to enable the integration matrix; it is skipped otherwise.

## Adding an engine

Each supported engine is a frozen `EngineSpec` dataclass in `schemapilot/engines/`. Adding one means registering a spec, not refactoring a dispatch chain.

1. Create a `YourEngineSpec(EngineSpec)` in `schemapilot/engines/`. The key fields:

   | Field | Purpose |
   |---|---|
   | `connection_fields` | Ordered list of `(key, prompt)` pairs the `--add-conn` flow asks for |
   | `sqlalchemy_url_driver` | Dialect+driver name for `create_engine` |
   | `sqlglot_dialect` | Dialect name sqlglot uses to parse/validate |
   | `build_uri(config)` | Returns a `sqlalchemy.URL` from the connection config dict |
   | `connect_args(config)` | Dict of kwargs for `create_engine`'s `connect_args` |
   | `engine_kwargs(config)` | Dict for `create_engine` kwargs (e.g. `poolclass`) |
   | `readonly_nodes` | sqlglot AST node classes the sentry allows at top level |
   | `readonly_commands` | Strings the sentry allows inside `Command` nodes (e.g. `"SHOW"`) |
   | `dangerous_functions` | Lowercase function names blocked anywhere in the AST |
   | `dangerous_nodes` | Node classes blocked anywhere in the AST |
   | `introspection_namespace_field` | `"schema"`, `"database"`, or `None` |

2. Register it in `registry.py` — add the class to `SPECS` and any short-name aliases to `ALIASES`.

3. Add the pip extra to `pyproject.toml` under `[project.optional-dependencies]`.

4. Add a seed fixture under `tests/fixtures/` and a container to `docker-compose.yml`.

5. Add entries to the `ENGINE_MATRIX` in `tests/integration/test_engine_matrix.py`.

### Finding the right sqlglot AST node class

```bash
python3 -c "import sqlglot; print(sqlglot.parse_one('SHOW TABLES').__class__.__name__)"
```

Run every read-only statement your engine supports through this one-liner. Anything that produces a class not in `readonly_nodes` or a `Command` whose `this` string is not in `readonly_commands` will be refused.

## Security sentry contract

The sentry is an **allowlist**. If a statement is not positively recognised as read-only for the active engine, it is refused. This means:

- Every new engine MUST populate `readonly_nodes` and `readonly_commands`. An empty list means every statement is blocked.
- Add every destructive function/command your engine exposes to `dangerous_functions` or `dangerous_nodes`, in lowercase.
- Do not assume a node class is safe just because `Select` is. The audit in this repo found `exp.Alias` hiding `RESET MASTER`, `CLUSTER`, and `REINDEX`; `exp.Command` hiding `OPTIMIZE TABLE` and `SYSTEM FLUSH LOGS`; and `exp.Into` hiding `SELECT INTO` (PostgreSQL `CREATE TABLE AS`).
- Add tests to `tests/test_security.py` for every new engine's policy.
- The 4 pre-existing security tests at the top of `tests/test_security.py` must remain unmodified and green.

## PR checklist

- [ ] Unit tests pass: `make test`
- [ ] Integration tests pass: `make test-integration` (Docker sandbox up)
- [ ] The 4 pre-existing tests in `tests/test_security.py` are unmodified and green
- [ ] No new dead dependencies — every `pip install` dep has a consumer in the codebase
- [ ] No new top-level files in `schemapilot/` without a clear owner
- [ ] New engine: spec registered, pip extra added, fixture seeded, integration entry added

## Code style

- 100-char line width.
- Use `rich.markup.escape()` on any string that could contain `[brackets]` before passing it to a Rich `Console` method — Rich interprets `[text]` as markup and silently deletes it. This applies to table names, column names, error messages, and anything user-provided.
- Table-name resolution goes through `schemapilot.names` — do not write a new leaf-extraction or suffix-match function. If something is ambiguous, surface the candidates rather than silently picking one.
- Preference for frozen dataclasses over dicts for structured data; preference for explicit shape checks over `hasattr` for engine dispatch.
