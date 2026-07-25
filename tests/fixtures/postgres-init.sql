-- SchemaPilot integration fixture: PostgreSQL.
--
-- Kept deliberately identical in shape to mysql-init.sql / clickhouse-init.sql and to the
-- in-process SQLite/DuckDB fixtures in tests/conftest.py, so ONE parametrised test body in
-- tests/integration/test_engine_matrix.py works on every engine:
--   customers(id PK, name, country)
--   orders(id PK, customer_id FK -> customers.id, amount, status)
--
-- Runs once, via /docker-entrypoint-initdb.d/, in the POSTGRES_DB database.

CREATE TABLE IF NOT EXISTS customers (
    id      INTEGER      PRIMARY KEY,
    name    VARCHAR(100) NOT NULL,
    country VARCHAR(2)   NOT NULL
);

CREATE TABLE IF NOT EXISTS orders (
    id          INTEGER        PRIMARY KEY,
    customer_id INTEGER        NOT NULL REFERENCES customers (id),
    amount      NUMERIC(10, 2) NOT NULL,
    status      VARCHAR(20)    NOT NULL
);

INSERT INTO customers (id, name, country) VALUES
    (1, 'Ada Lovelace',   'GB'),
    (2, 'Grace Hopper',   'US'),
    (3, 'Kathleen Booth', 'GB')
ON CONFLICT (id) DO NOTHING;

INSERT INTO orders (id, customer_id, amount, status) VALUES
    (1, 1, 120.50, 'shipped'),
    (2, 1,  75.00, 'pending'),
    (3, 2, 310.25, 'shipped'),
    (4, 3,  42.00, 'cancelled')
ON CONFLICT (id) DO NOTHING;
