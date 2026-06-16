INSERT INTO dbo.weaver_fixture_customers (
    customer_id,
    customer_name,
    region_code,
    signup_date,
    is_active
)
VALUES
    (1, 'Ada Lovelace', 'AU', '2026-01-05', 1),
    (2, 'Grace Hopper', 'NZ', '2026-02-12', 1),
    (3, 'Katherine Johnson', 'AU', '2025-11-20', 0),
    (4, 'Mary Jackson', 'US', '2026-03-03', 1);

INSERT INTO dbo.weaver_fixture_products (
    product_id,
    sku,
    category_code,
    unit_price,
    is_active
)
VALUES
    (10, 'SKU-ALPHA', 'BOOKS', 25.50, 1),
    (11, 'SKU-BETA', 'BOOKS', 18.75, 1),
    (12, 'SKU-GAMMA', 'TOOLS', 120.00, 1),
    (13, 'SKU-DELTA', 'ARCHIVE', 9.99, 0);

INSERT INTO dbo.weaver_fixture_orders (
    order_id,
    customer_id,
    order_date,
    status_code,
    channel_code
)
VALUES
    (100, 1, '2026-03-15', 'COMPLETE', 'WEB'),
    (101, 1, '2026-03-20', 'COMPLETE', 'STORE'),
    (102, 2, '2026-04-02', 'PENDING', 'WEB'),
    (103, 4, '2026-04-05', 'COMPLETE', 'WEB');

INSERT INTO dbo.weaver_fixture_order_lines (
    order_line_id,
    order_id,
    product_id,
    quantity,
    discount_rate
)
VALUES
    (1000, 100, 10, 2, 0.00),
    (1001, 100, 12, 1, 0.10),
    (1002, 101, 11, 3, NULL),
    (1003, 102, 10, 1, 0.05),
    (1004, 103, 12, 2, 0.15);
