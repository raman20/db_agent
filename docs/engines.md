---
title: Engine Reference
---

# Engine Reference

SchemaPilot supports six SQL engines. Each is described by a declarative spec — adding a new one is a data file, not a refactor.

## Supported engines

| Engine | Install | Port | Transaction support | Notes |
|---|---|---|---|---|
| SQLite | included | — | Yes (file) | File path; no server needed |
| PostgreSQL | included | 5432 | Yes | Full PK/FK metadata |
| MySQL | included | 3306 | Yes | Full PK/FK metadata |
| DuckDB | `pip install 'schemapilot[duckdb]'` | — | Yes (file) | File path or `:memory:`; no PK reflection |
| Trino | `pip install 'schemapilot[trino]'` | 8080 | No | 3-part names; HTTPS with optional CA bundle |
| ClickHouse | `pip install 'schemapilot[clickhouse]'` | 8123 (HTTP) | No | Fast analytics; no FK metadata |

`pip install 'schemapilot[all]'` installs all optional drivers. Run `/engines` in the REPL to see which drivers are available and the exact `pip` command for any that are missing.

## Engine setup

### SQLite

```bash
schemapilot --add-conn
# Name: my-sqlite
# Engine: sqlite
# Path: /home/user/mydb.sqlite
```

No server, no Docker. The file path is all you need.

### PostgreSQL

```bash
schemapilot --add-conn
# Name: prod-postgres
# Engine: postgres
# Host: localhost
# Port: 5432
# Database: myapp
# Username: readonly_user
# Password: ...
```

SchemaPilot introspects schemas, tables, columns, primary keys, and foreign keys. The configured schema (from the connection profile) wins over enumerating `get_schema_names()`, so a connection to a busy Postgres server with hundreds of schemas is not overwhelmed.

### MySQL

```bash
schemapilot --add-conn
# Name: prod-mysql
# Engine: mysql
# Host: localhost
# Port: 3306
# Database: myapp
# Username: readonly_user
# Password: ...
```

Works identically to PostgreSQL from the user's perspective.

### DuckDB

```bash
pip install 'schemapilot[duckdb]'
schemapilot --add-conn
# Name: analytics
# Engine: duckdb
# Path: /home/user/analytics.duckdb
```

**Known limitation:** DuckDB's SQLAlchemy driver does not expose primary key metadata through `get_pk_constraint()`, so `/schema` will not show PK markers and the agent prompt will not tag columns as primary keys. This is an upstream driver limitation tracked in the test suite.

DuckDB uses composite schema names (e.g. `my_db.main`), which SchemaPilot handles correctly during sampling and schema display.

### Trino

```bash
pip install 'schemapilot[trino]'
schemapilot --add-conn
# Name: data-lake
# Engine: trino
# Host: trino.example.com
# Port: 8080
# Catalog: tpch
# Schema: tiny
# Username: user
# Password: ...
# TLS CA bundle path (optional): /path/to/ca.pem
```

Trino uses 3-part table names: `catalog.schema.table`. SchemaPilot preserves all three parts in the catalog, the REPL, and agent prompts.

**HTTPS / TLS:** If your Trino coordinator uses a self-signed or internal CA certificate, set the CA bundle path during `--add-conn`. SchemaPilot passes it as `verify` in the connection args. The connection is tested on save.

**No transactions:** Trino is a distributed query engine without transactional semantics. `/engines` will show "safe dry-run: unavailable" — `EXPLAIN` is refused on all engines for security reasons regardless.

### ClickHouse

```bash
pip install 'schemapilot[clickhouse]'
schemapilot --add-conn
# Name: events-db
# Engine: clickhouse
# Host: localhost
# Port: 8123
# Database: default
# Username: default
# Password: ...
```

ClickHouse uses the HTTP interface (port 8123) through `clickhouse-connect`. Reflection is bound to a connection, not an engine — a bug fixed during the multi-engine refactor that had caused zero tables to be returned.

**No FK metadata:** ClickHouse does not enforce foreign keys, and `get_pk_constraint()` reports empty constraints, so `/schema` shows PK markers only when the driver provides them.

**No transactions:** Like Trino, ClickHouse is not transactional. The sentry still default-denies mutations.

## Engine quirks round-up

| Behaviour | SQLite | Postgres | MySQL | DuckDB | Trino | ClickHouse |
|---|---|---|---|---|---|---|
| PK metadata | Yes | Yes | Yes | No | Partial | No |
| FK metadata | Yes | Yes | Yes | Yes | No | No |
| Transactions | Yes | Yes | Yes | Yes | No | No |
| Safe dry-run | EXPLAIN refused | EXPLAIN refused | EXPLAIN refused | EXPLAIN refused | EXPLAIN refused | EXPLAIN refused |
| Cross-schema FK | No | Yes | Yes | No | Yes | — |
| SQL naming parts | 1 | 2 | 2 | 2 (composite) | 3 | 2 |

"Safe dry-run: refused" means `EXPLAIN` is blocked on every engine. This is intentional: on PostgreSQL, `EXPLAIN ANALYZE DELETE FROM users` genuinely executes the delete, and the statement's contents are invisible to the parser.

## Adding a new engine

See [CONTRIBUTING.md](https://github.com/raman20/SchemaPilot/blob/main/CONTRIBUTING.md#adding-an-engine) for the step-by-step guide. The core contract: subclass `EngineSpec`, define your security policy (`readonly_nodes`, `dangerous_functions`), register the spec, add a pip extra, and seed a fixture.
