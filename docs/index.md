---
title: SchemaPilot
---

# SchemaPilot — AI Copilot for SQL

SchemaPilot is an interactive terminal CLI that translates natural language questions into validated SQL, executes them against your database, and prints formatted results — all locally, with no web server wrappers.

## What it does

- **Ask questions in plain English.** "Which customers spent more than $100 last month?" SchemaPilot generates the SQL, checks it against a per-engine security sentry, runs it, and explains the answer.
- **Six SQL engines.** PostgreSQL, MySQL, SQLite, DuckDB, Trino, and ClickHouse — each with a declarative spec, so adding another is a data file, not a refactor.
- **Security sentry.** Every query is parsed into an AST and checked against an engine-specific allowlist. Mutations are blocked. Read-shaped SQL that reaches the filesystem is flagged. The sentry is a guard, not a sandbox — least-privilege credentials are the real boundary.
- **Interactive REPL.** A persistent shell with command history, tab completion over your real table names, and `@table` pinning to steer the model when it picks the wrong table.
- **Token-cost aware.** Prompts carry compact metadata rather than full DDL, tables are shortlisted lexically, and sample data rows are opt-in — so you are not paying to send every column of every table on every question.

## Quick links

- [Usage guide](usage) — quickstart, REPL commands, @table pinning
- [Engine reference](engines) — setup per engine, pip extras, quirks
- [Security model](security) — what the sentry blocks and what it does not
- [README](https://github.com/raman20/SchemaPilot#readme) — installation, credential storage, development
- [Contributing](https://github.com/raman20/SchemaPilot/blob/main/CONTRIBUTING.md) — how to add an engine, PR checklist
