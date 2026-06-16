CREATE TABLE dbo.weaver_fixture_customers (
    customer_id int NOT NULL,
    customer_name varchar(100) NOT NULL,
    region_code varchar(10) NOT NULL,
    signup_date date NOT NULL,
    is_active bit NOT NULL
);

CREATE TABLE dbo.weaver_fixture_products (
    product_id int NOT NULL,
    sku varchar(30) NOT NULL,
    category_code varchar(30) NOT NULL,
    unit_price decimal(12,2) NOT NULL,
    is_active bit NOT NULL
);

CREATE TABLE dbo.weaver_fixture_orders (
    order_id int NOT NULL,
    customer_id int NOT NULL,
    order_date date NOT NULL,
    status_code varchar(20) NOT NULL,
    channel_code varchar(20) NOT NULL
);

CREATE TABLE dbo.weaver_fixture_order_lines (
    order_line_id int NOT NULL,
    order_id int NOT NULL,
    product_id int NOT NULL,
    quantity int NOT NULL,
    discount_rate decimal(5,2) NULL
);
