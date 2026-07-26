# SchemaPilot

[![CI](https://github.com/raman20/SchemaPilot/actions/workflows/ci.yml/badge.svg)](https://github.com/raman20/SchemaPilot/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12-blue)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)

SchemaPilot is an interactive, local-only terminal CLI copilot for anything that speaks SQL. It
translates natural language questions into validated SQL, executes them against your target
engine, and returns formatted analysis and tabular results directly in your console.

It is designed to run completely on your local machine, using either cloud LLMs (Gemini, OpenAI,
Claude) or local offline models (Ollama, LlamaEdge) without requiring any web server wrappers.

## Supported engines

| Engine | Install | Notes |
|---|---|---|
| PostgreSQL | included | |
| MySQL | included | |
| SQLite | included | file path, no server |
| DuckDB | `pip install 'schemapilot[duckdb]'` | file path or `:memory:`; no PK metadata via reflection |
| Trino | `pip install 'schemapilot[trino]'` | 3-part `catalog.schema.table` names; no FK metadata |
| ClickHouse | `pip install 'schemapilot[clickhouse]'` | no FK metadata |

`pip install 'schemapilot[all]'` installs every optional driver. Each engine is described by a
declarative spec in `schemapilot/engines/`, so adding another SQL-speaking engine is a small
data file rather than a refactor. All six above are verified against real servers in CI-style
integration tests (`make test-integration`), not contract tests alone.

Run `/engines` inside the REPL to see which drivers are installed and the exact `pip` command
for any that are missing.

## Demo

```
❯ /engines
┏━━━┳━━━━━━━━━━━━┳━━━━━━━━━━━┳━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━┓
┃   ┃ Engine     ┃ Driver    ┃ Safe dry-run ┃ Cross-engine reach        ┃ Fix / note ┃
┡━━━╇━━━━━━━━━━━━╇━━━━━━━━━━━╇━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━┩
│   │ ClickHouse │ installed │ unavailable  │ —                         │            │
│   │ DuckDB     │ installed │ available    │ —                         │            │
│   │ MySQL      │ installed │ available    │ —                         │            │
│   │ PostgreSQL │ installed │ available    │ —                         │            │
│ ● │ SQLite     │ installed │ available    │ —                         │            │
│   │ Trino      │ installed │ unavailable  │ configured Trino catalogs │            │
└───┴────────────┴───────────┴──────────────┴───────────────────────────┴────────────┘

❯ /schema customers
customers
├── id: INTEGER (PK)
├── name: TEXT NOT NULL
└── country: TEXT

❯ /sql SELECT id, name FROM customers LIMIT 3
┏━━━━┳━━━━━━━┓
┃ id ┃ name  ┃
┡━━━━╇━━━━━━━┩
│ 1  │ Alice │
│ 2  │ Bob   │
│ 3  │ Carol │
└────┴───────┘
```

See the full transcript at [docs/demo.txt](docs/demo.txt).

## Core Features

* **Interactive REPL Shell:** A persistent query shell with up/down arrow command history search (backed by `prompt_toolkit`).
* **Global Model Profiles:** Manage credentials for multiple LLMs and switch active models dynamically.
* **Global Database Profiles:** Manage credentials for all six supported engines.
* **AST SQL Security Sentry:** Validates query safety prior to execution using Abstract Syntax Tree (AST) analysis via `sqlglot`. It is a per-engine *allowlist*: anything it does not positively recognise as read-only is refused. See [Security model](#security-model) for what that does and does not protect.
* **Schema-aware REPL:** Eight slash commands (`/help`, `/use`, `/engines`, `/tables`, `/schema`, `/sql`, `/why`, `/exit`) with tab completion over your real table names, plus `@table` to pin a table into the prompt.
* **Token-cost controls:** Prompts carry compact metadata rather than full DDL, tables are shortlisted lexically with one hop of foreign-key expansion, and sample data rows are **opt-in per connection** rather than sent by default.
* **Dynamic Step Log Spinner:** Employs `rich` console status spinners to track agent states (Architect, Programmer, Sentry, Executor, Analyst) in real time.
* **Formatted Outputs:** Visualizes database queries using SQL syntax-highlighting panels, markdown tables, and formatted terminal data grids.

---

## Directory Layout

```
schema-pilot/
├── schemapilot/           # Core library package
│   ├── __init__.py
│   ├── config.py          # Environment defaults
│   ├── db.py              # Connections registry (~/.config/schemapilot/connections.json)
│   ├── security.py        # SQL AST validation rules
│   ├── llm.py             # LLM profiles registry (~/.config/schemapilot/models.json)
│   └── agent.py           # SQL generation and evaluation pipeline
│   ├── cli.py             # CLI entrypoint driver (schemapilot.cli:main)
│   ├── catalog.py         # In-memory schema cache (sole owner of metadata)
│   ├── pruning.py         # Lexical table shortlisting for prompts
│   ├── engines/           # Declarative EngineSpec per supported engine
│   └── repl/              # Session, command dispatch, completion
├── tests/                 # Unit tests
│   ├── test_security.py   # Security AST test suite
│   └── integration/       # Live six-engine matrix (SCHEMAPILOT_IT=1)
├── pyproject.toml         # Python packaging metadata (PEP 517/621)
├── requirements.txt       # Project dependencies
├── Makefile               # Task runner definitions
└── docker-compose.yml     # Local sandboxes (Postgres, MySQL, Trino, ClickHouse)
```

---

## Setup & Quickstart

### 1. Initialize Sandbox Databases
Spin up the PostgreSQL, MySQL, Trino and ClickHouse sandbox containers:
```bash
make sandbox
```
*Trino takes 30-60 seconds to become healthy on a cold start.*

### 2. Install

```bash
# Recommended: pipx (isolated environment, auto-PATH)
pipx install schemapilot          # base install
pipx install 'schemapilot[all]'   # all engines

# Or: pip
pip install schemapilot
pip install 'schemapilot[all]'

# Or: from source
git clone https://github.com/raman20/SchemaPilot.git && cd SchemaPilot && make install
```

### 3. Add a Database Connection Profile
Configure a connection profile for any supported engine. SchemaPilot prompts only for the fields
that engine actually needs — a file path for SQLite/DuckDB, host/port/credentials for the rest,
plus catalog and schema for Trino:
```bash
schemapilot --add-conn
```
*Follow the interactive prompt. On save, SchemaPilot will automatically test database connectivity.*

### 4. Add an AI Model Profile
Add credentials for Gemini, OpenAI, Claude, an OpenAI-compatible endpoint, or Ollama:
```bash
schemapilot --add-model
```

### 5. Switch Active Profiles
```bash
# Toggle active database connections
schemapilot --list-conns
schemapilot --select-conn <connection_id>

# Toggle active model configurations
schemapilot --list-models
schemapilot --select-model <model_id>
```

---

## Usage Examples

### Natural Language Queries
Execute a single query against your active connection:
```bash
schemapilot "Show total orders by month"
```

### Interactive Console
Start the persistent prompt shell:
```bash
schemapilot
```

Inside the shell, bare text is a natural-language question. Slash commands:

| Command | What it does |
|---|---|
| `/help [cmd]` | List commands, or explain one |
| `/use <name>` | Switch active connection and warm its schema cache |
| `/engines` | Driver status per engine, and the exact `pip` fix if one is missing |
| `/tables [glob]` | List tables, optionally filtered |
| `/schema [table]` | Schema tree with primary-key and foreign-key markers |
| `/sql <raw>` | Run raw SQL with no LLM — still checked by the sentry |
| `/why` | Explain which tables were shortlisted for the last question, and why |
| `/exit` | Leave the shell |

Typing `@` in a question completes a table name **and pins that table** into the prompt, which is
the fastest way to correct a wrong table choice.

### Direct Arguments Override
Override active profile configurations for a single execution:
```bash
schemapilot --provider openai --model gpt-4o --api-key sk-proj-12345 "List all tables"
```

---

## CLI Reference

```text
positional arguments:
  query                 Single natural language query to execute

options:
  -h, --help            show this help message and exit
  --add-conn            Interactively add and test a new database profile link
  --list-conns          List all saved database connection profiles
  --select-conn CONN_ID Set the active database profile by its ID
  --delete-conn CONN_ID Delete a database connection profile by its ID
  --add-model           Interactively add a new AI model profile configuration
  --list-models         List all saved AI model configurations
  --select-model MODEL_ID Set the active AI model profile by its ID
  --delete-model MODEL_ID Delete an AI model profile configuration by its ID
  --save-llm            Persist model settings passed as command overrides
  --provider PROVIDER   AI provider override (google, openai, anthropic, local)
  --model MODEL         AI model name override (e.g. gemini-2.0-flash, gpt-4o)
  --api-key API_KEY     API authentication key override
  --base-url BASE_URL   Local server endpoint base URL override (Ollama / LM Studio)
  --version             Show the SchemaPilot version number and exit
  --install-completion  Print shell eval line for tab completion (bash / zsh)
```

## Documentation

- [Usage guide](docs/usage.md) — quickstart, REPL commands, @table pinning
- [Engine reference](docs/engines.md) — setup per engine, pip extras, quirks
- [Security model](docs/security.md) — what the sentry blocks and what it does not
- [Contributing](CONTRIBUTING.md) — how to add an engine, PR checklist

---

## Credential Storage

Connection profiles live in `~/.config/schemapilot/connections.json` and model profiles in
`~/.config/schemapilot/models.json`. **Both store secrets in plaintext at rest** — database
passwords and LLM API keys respectively.

SchemaPilot mitigates this as far as file permissions allow: the directory is created `0700`
and both files are written atomically with mode `0600`, so they are readable only by your user
account. That is *not* encryption — anything running as your user can read them, and so can
anyone with your backups. Prefer least-privilege, read-only database credentials.

Storing secrets in the OS keyring (Keychain / Secret Service / Credential Manager) is planned
future work.

---

## Security model

The sentry is a **DDL/DML guard, not a sandbox.** It parses every statement with `sqlglot` and
refuses anything it cannot positively classify as read-only for the active engine, which blocks
`DROP`, `ALTER`, `CREATE`, `TRUNCATE`, `MERGE`, `GRANT`, `ATTACH`, `SELECT ... INTO`, multi-statement
payloads such as `SELECT 1; DROP TABLE users`, and per-engine hazards like ClickHouse's
`SYSTEM`/`OPTIMIZE` or DuckDB's `read_csv()`.

What it deliberately does **not** claim:

* **It is not containment.** Read-shaped SQL can still reach the filesystem or network through
  engine table functions. SchemaPilot blocks the ones it knows about, which raises the bar but is
  not a boundary. **Least-privilege, read-only database credentials are the real control.**
* **`EXPLAIN` is refused on every engine.** This is intentional: on PostgreSQL,
  `EXPLAIN ANALYZE DELETE FROM users` genuinely executes the delete, and the statement's contents
  are invisible to the parser. Allowing `EXPLAIN` by keyword would let a read-only session
  modify data.
* **The v1 REPL never writes.** Every query runs with mutations disabled, regardless of
  configuration. Interactive mutation approval is planned future work.

## Development & Testing

Run the unit suite:
```bash
make test
```

Run the live six-engine integration matrix (requires `make sandbox` first; skipped unless
`SCHEMAPILOT_IT=1`):
```bash
make test-integration
```