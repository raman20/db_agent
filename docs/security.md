---
title: Security Model
---

# Security Model

SchemaPilot's sentry is a **DDL/DML guard**, not a sandbox. It parses every SQL statement into an Abstract Syntax Tree (AST) using `sqlglot` and refuses anything it cannot positively classify as read-only for the active engine.

## How it works

1. The SQL string is parsed into an AST by `sqlglot`, using the engine's dialect (e.g. Trino SQL, MySQL SQL, ClickHouse SQL).
2. The top-level statement is classified against an engine-specific **allowlist** of AST node classes. If the statement's root node is not in the allowlist, it is refused.
3. Even if the root node is allowed, the sentry walks every child node looking for **dangerous functions** (e.g. `pg_read_file`, `read_csv`, `url`) and **dangerous node types** (e.g. `Drop`, `Delete`, `Insert`, `TruncateTable`, `Into`, `Lock`).
4. Multi-statement payloads (`SELECT 1; DROP TABLE users`) are detected and refused — the sentry examines every statement in a `Block` node individually.
5. `Command` nodes (like `SHOW`, `EXPLAIN`, `SYSTEM`, `OPTIMIZE`) are checked against a per-engine allowlist of allowed command keywords. Trino's `SHOW CATALOGS` is allowed; ClickHouse's `SYSTEM FLUSH LOGS` is not.

The sentry **default-denies**: if an engine is unknown or its spec is missing, all statements are refused. This prevents a typo in the engine name from silently downgrading to a weaker policy.

## What it blocks per engine

| Threat | SQLite | Postgres | MySQL | DuckDB | Trino | ClickHouse |
|---|---|---|---|---|---|---|
| `DROP TABLE` / `DROP VIEW` / `DROP SCHEMA` | Blocked | Blocked | Blocked | Blocked | Blocked | Blocked |
| `DELETE FROM` | Blocked | Blocked | Blocked | Blocked | Blocked | Blocked |
| `INSERT INTO` | Blocked | Blocked | Blocked | Blocked | Blocked | Blocked |
| `UPDATE` | Blocked | Blocked | Blocked | Blocked | Blocked | Blocked |
| `ALTER TABLE` | Blocked | Blocked | Blocked | Blocked | Blocked | Blocked |
| `CREATE TABLE` / `CREATE VIEW` | Blocked | Blocked | Blocked | Blocked | Blocked | Blocked |
| `TRUNCATE TABLE` | Blocked | Blocked | Blocked | Blocked | Blocked | Blocked |
| `MERGE` | Blocked | Blocked | Blocked | Blocked | Blocked | Blocked |
| `GRANT` / `REVOKE` | Blocked | Blocked | Blocked | Blocked | Blocked | Blocked |
| `SELECT ... INTO` | Blocked | Blocked | Blocked | Blocked | Blocked | Blocked |
| `SELECT ... FOR UPDATE` | Blocked | Blocked | Blocked | Blocked | Blocked | Blocked |
| `EXPLAIN` / `EXPLAIN ANALYZE` | Blocked | Blocked | Blocked | Blocked | Blocked | Blocked |
| Multi-statement (`; DROP ...`) | Blocked | Blocked | Blocked | Blocked | Blocked | Blocked |
| `read_csv` / `read_parquet` / `glob` / `url` / `s3` | Blocked | — | — | Blocked | — | — |
| `pg_read_file` / `pg_write_file` / `pg_ls_dir` | — | Blocked | — | — | — | — |
| `SYSTEM ...` / `OPTIMIZE TABLE` | — | — | — | — | — | Blocked |
| `RESET MASTER` / `FLUSH TABLES` | — | — | Blocked | — | — | — |
| `REINDEX` / `CLUSTER` | Blocked | Blocked | — | — | — | — |
| Writable PRAGMAs | Blocked | — | — | Blocked | — | — |

## The EXPLAIN limitation

`EXPLAIN` is refused on every engine. The reason: on PostgreSQL, `EXPLAIN ANALYZE DELETE FROM users` genuinely executes the `DELETE` — it returns a query plan for a statement it actually ran. The parser sees an `EXPLAIN` command with a text literal payload; the `DELETE` inside is invisible to the AST. Allowing `EXPLAIN` by keyword would let a read-only session modify data.

This is a known, deliberate trade-off. If you need `EXPLAIN`, run it directly in your database client.

## Read-only v1 REPL

Every query in the REPL runs with mutations disabled (`allow_mutation=False`), regardless of the `ALLOW_MUTATING_QUERIES` env var or any configuration. Interactive mutation approval (a panel that shows the diff and asks "run this?") is planned future work.

## Credential storage

Connection profiles (`~/.config/schemapilot/connections.json`) and model profiles (`~/.config/schemapilot/models.json`) store secrets in plaintext at rest — database passwords and LLM API keys respectively.

SchemaPilot mitigates this as far as file permissions allow:

- The config directory is created with mode `0700` (owner-only access).
- Both credential files are written atomically (write to temp file, `fsync`, `mv`) with mode `0600`.

This is **not encryption**. Anything running as your user can read these files, and so can anyone with your backups.

**Recommendations:**

- Use **least-privilege, read-only database credentials** for every connection profile. The sentry is a guard, not a sandbox — a read-shaped query can still reach the filesystem or network through engine table functions.
- Store API keys that have usage limits rather than unrestricted keys.
- OS keyring integration (macOS Keychain, Linux Secret Service, Windows Credential Manager) is planned future work.

## Reporting a vulnerability

If you find a statement that the sentry allows but that modifies data, please open a GitHub issue with the exact SQL, the engine, and the sqlglot version (`pip show sqlglot`). Do not post API keys or passwords. SchemaPilot does not have a separate security contact yet — the public issue tracker is the right place.
