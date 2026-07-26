---
title: Usage Guide
---

# Usage Guide

## Quickstart

```bash
pip install schemapilot              # base install (sqlite, postgres, mysql)
pip install 'schemapilot[all]'       # all six engines
schemapilot --add-conn               # add a database connection
schemapilot --add-model              # add an LLM profile
schemapilot "list all customers"     # ask a question
```

### 1. Add a database connection

`schemapilot --add-conn` walks you through an interactive prompt. SchemaPilot asks only for the fields your engine needs:

| Engine | What you provide |
|---|---|
| SQLite | File path |
| DuckDB | File path or `:memory:` |
| PostgreSQL | Host, port, database, user, password |
| MySQL | Host, port, database, user, password |
| ClickHouse | Host, port, database, user, password |
| Trino | Host, port, catalog, schema, user, password, optional CA bundle path |

The connection is tested on save. Profiles are stored in `~/.config/schemapilot/connections.json`.

### 2. Add an LLM profile

`schemapilot --add-model` prompts for provider, model name, API key, and base URL (for openai-compatible / ollama). Supported providers: Google, OpenAI, Anthropic, OpenAI-compatible, Ollama, local (generic).

Profiles are stored in `~/.config/schemapilot/models.json`. Switch active profiles with `--select-model`.

### 3. Ask a question

```bash
schemapilot "Show total orders by month"
```

SchemaPilot generates SQL, validates it, executes it, and prints:

- The generated SQL (syntax-highlighted)
- The result rows (formatted table)
- An analysis summary (what the data means)

Override LLM settings for a single execution:

```bash
schemapilot --provider openai --model gpt-4o "what are the top 5 products?"
```

## The REPL

Start the persistent shell with `schemapilot` (no arguments). Inside the shell, bare text is a natural-language question.

### Commands

| Command | What it does |
|---|---|
| `/help [cmd]` | List all commands, or explain one in detail |
| `/use <name>` | Switch active connection and warm the schema cache — type a partial name and it auto-selects the match |
| `/engines` | Driver status per engine, and the exact `pip install` command if one is missing |
| `/tables [glob]` | List cached tables, optionally filtered (e.g. `/tables cust*`) |
| `/schema [table]` | Rich-rendered schema tree with primary-key and foreign-key markers |
| `/sql <raw>` | Run raw SQL — still checked by the sentry, so `DELETE` / `DROP` / `ALTER` are refused |
| `/why` | Explain which tables were sent to the model for the last question, and why each was picked |
| `/exit` | Leave the shell |

### Pinning tables with `@`

The agent picks tables by matching question words against table names and column names. When it picks wrong, prefix the correct table name with `@`:

```
@products show me the top 5 by revenue
```

The `@products` pin forces `products` into the prompt regardless of what the pruner selected. After asking, `/why` confirms the pin was honoured.

If a pin is ambiguous (e.g. `@orders` when both `sales.orders` and `archive.orders` exist), `/why` lists the colliding tables so you can qualify it: `@sales.orders`.

### Tab completion

SchemaPilot completes table names after `@` and after `/schema` — type a few characters and press Tab. The completer does zero I/O: it reads from the in-memory schema cache that `/use` warms.

To enable shell-level tab completion for the `schemapilot` command itself:

```bash
schemapilot --install-completion
```

This prints the `eval` line to add to your `~/.bashrc` or `~/.zshrc`. Requires `pip install argcomplete`.

## LLM providers

| Provider | CLI flag | Environment variable | Notes |
|---|---|---|---|
| Google (Gemini) | `--provider google` | `LLM_PROVIDER=google` | Free tier available |
| OpenAI | `--provider openai` | `LLM_PROVIDER=openai` | |
| Anthropic (Claude) | `--provider anthropic` | `LLM_PROVIDER=anthropic` | |
| OpenAI-compatible | `--provider openai-compatible` | `LLM_PROVIDER=openai-compatible` | Any compatible endpoint (LM Studio, vLLM, etc.) |
| Ollama | `--provider ollama` | `LLM_PROVIDER=ollama` | Local models, free |
| Local (generic) | `--provider local` | `LLM_PROVIDER=local` | Any OpenAI-compatible local server |

Set `LLM_MODEL_NAME`, `LLM_API_KEY`, `LLM_BASE_URL`, and `LLM_TEMPERATURE` in your environment or in `~/.config/schemapilot/models.json`.

## Environment variables

Copy `.env.example` to `.env` and fill in your values. SchemaPilot loads `.env` from the project root and from the current working directory (cwd wins). Every supported variable is documented in `.env.example`.
