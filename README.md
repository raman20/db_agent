# SchemaPilot

SchemaPilot is an interactive, local-only terminal CLI copilot for relational databases. It translates natural language questions into secure, optimized SQL, executes them against your target database, and returns formatted analysis and tabular results directly in your console. 

It is designed to run completely on your local machine, using either cloud LLMs (Gemini, OpenAI, Claude) or local offline models (Ollama, LlamaEdge) without requiring any web server wrappers.

## Core Features

* **Interactive REPL Shell:** A persistent query shell with up/down arrow command history search (backed by `prompt_toolkit`).
* **Global Model Profiles:** Manage credentials for multiple LLMs and switch active models dynamically.
* **Global Database Profiles:** Manage credentials for MySQL, PostgreSQL, and SQLite databases.
* **AST SQL Security Sentry:** Validates query safety prior to execution using Abstract Syntax Tree (AST) analysis via `sqlglot` to block destructive DDL/DML mutations.
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
├── tests/                 # Unit tests
│   └── test_security.py   # Security AST test suite
├── cli.py                 # CLI entrypoint driver
├── pyproject.toml         # Python packaging metadata (PEP 517/621)
├── requirements.txt       # Project dependencies
├── Makefile               # Task runner definitions
└── docker-compose.yml     # Local database sandboxes (MySQL, Postgres)
```

---

## Setup & Quickstart

### 1. Initialize Sandbox Databases
Spin up the PostgreSQL and MySQL sandbox containers:
```bash
make sandbox
```

### 2. Local Installation
Install the project in editable mode to register the `schemapilot` binary globally in your shell path:
```bash
make install
```

### 3. Add a Database Connection Profile
Configure a connection profile for PostgreSQL, MySQL, or SQLite:
```bash
schemapilot --add-conn
```
*Follow the interactive prompt. On save, SchemaPilot will automatically test database connectivity.*

### 4. Add an AI Model Profile
Add credentials for Gemini, OpenAI, Claude, or a local Ollama server:
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
```

---

## Development & Testing

Run the security sentinel test suite:
```bash
PYTHONPATH=. pytest
```