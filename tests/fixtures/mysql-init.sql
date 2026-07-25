-- SchemaPilot integration fixture: MySQL.
--
-- Same shape as postgres-init.sql / clickhouse-init.sql (see that file's header): a
-- customers/orders pair with a real PRIMARY KEY and FOREIGN KEY, so introspection has
-- both PK and relationship metadata to report.
--
-- Runs once, via /docker-entrypoint-initdb.d/, in the MYSQL_DATABASE database.

CREATE TABLE IF NOT EXISTS customers (
    id      INT          NOT NULL,
    name    VARCHAR(100) NOT NULL,
    country VARCHAR(2)   NOT NULL,
    PRIMARY KEY (id)
) ENGINE = InnoDB;

CREATE TABLE IF NOT EXISTS orders (
    id          INT            NOT NULL,
    customer_id INT            NOT NULL,
    amount      DECIMAL(10, 2) NOT NULL,
    status      VARCHAR(20)    NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT fk_orders_customer FOREIGN KEY (customer_id) REFERENCES customers (id)
) ENGINE = InnoDB;

INSERT IGNORE INTO customers (id, name, country) VALUES
    (1, 'Ada Lovelace',   'GB'),
    (2, 'Grace Hopper',   'US'),
    (3, 'Kathleen Booth', 'GB');

INSERT IGNORE INTO orders (id, customer_id, amount, status) VALUES
    (1, 1, 120.50, 'shipped'),
    (2, 1,  75.00, 'pending'),
    (3, 2, 310.25, 'shipped'),
    (4, 3,  42.00, 'cancelled');
