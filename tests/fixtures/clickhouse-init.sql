-- SchemaPilot integration fixture: ClickHouse.
--
-- Same customers/orders shape as postgres-init.sql and mysql-init.sql so one parametrised
-- integration test body works across every engine.
--
-- DIFFERENCE FROM THE RELATIONAL FIXTURES -- deliberate, not an oversight:
--   * ClickHouse has NO foreign keys and no enforced primary-key constraint. MergeTree's
--     ORDER BY key is the closest analogue (it is a sparse sorting key, not a uniqueness
--     constraint), so `orders.customer_id` is only a join column by convention.
--   * ClickHouse's SQLAlchemy dialect also stubs FK/index/view reflection to [] and
--     get_pk_constraint() to {"constrained_columns": [], "name": None}. The engine matrix
--     therefore asserts tables/columns here, never PK/FK metadata.
--   * No AUTO_INCREMENT / SERIAL and no ON CONFLICT: ids are supplied explicitly and the
--     tables are (re)created empty, so a repeated init cannot double-insert.
--
-- Runs once, via /docker-entrypoint-initdb.d/. Names are fully qualified because the
-- entrypoint's client session database has varied between image versions.

CREATE DATABASE IF NOT EXISTS schemapilot_db;

CREATE TABLE IF NOT EXISTS schemapilot_db.customers
(
    id      UInt32,
    name    String,
    country LowCardinality(String)
)
ENGINE = MergeTree
ORDER BY id;

CREATE TABLE IF NOT EXISTS schemapilot_db.orders
(
    id          UInt32,
    customer_id UInt32,   -- logical FK to customers.id; ClickHouse cannot enforce it
    amount      Decimal(10, 2),
    status      LowCardinality(String)
)
ENGINE = MergeTree
ORDER BY (customer_id, id);

TRUNCATE TABLE IF EXISTS schemapilot_db.customers;
TRUNCATE TABLE IF EXISTS schemapilot_db.orders;

INSERT INTO schemapilot_db.customers (id, name, country) VALUES
    (1, 'Ada Lovelace',   'GB'),
    (2, 'Grace Hopper',   'US'),
    (3, 'Kathleen Booth', 'GB');

INSERT INTO schemapilot_db.orders (id, customer_id, amount, status) VALUES
    (1, 1, 120.50, 'shipped'),
    (2, 1,  75.00, 'pending'),
    (3, 2, 310.25, 'shipped'),
    (4, 3,  42.00, 'cancelled');
