DECLARE @start_date date = '2026-03-01';

WITH recent_orders AS (
    SELECT
        o.order_id,
        o.customer_id,
        o.order_date,
        o.status_code
    FROM dbo.weaver_fixture_orders AS o
    WHERE
        o.order_date >= @start_date
        AND o.channel_code = 'WEB'
),
line_totals AS (
    SELECT
        ro.order_id,
        SUM(CAST(ol.quantity AS decimal(18,2)) * p.unit_price) AS order_amount
    FROM recent_orders AS ro
    INNER JOIN dbo.weaver_fixture_order_lines AS ol
        ON ol.order_id = ro.order_id
    INNER JOIN dbo.weaver_fixture_products AS p
        ON p.product_id = ol.product_id
    GROUP BY
        ro.order_id
)
SELECT
    ro.order_id,
    c.customer_name,
    ro.order_date,
    ro.status_code,
    lt.order_amount
FROM recent_orders AS ro
INNER JOIN dbo.weaver_fixture_customers AS c
    ON c.customer_id = ro.customer_id
INNER JOIN line_totals AS lt
    ON lt.order_id = ro.order_id
WHERE
    EXISTS (
        SELECT
            1
        FROM dbo.weaver_fixture_order_lines AS ol
        WHERE
            ol.order_id = ro.order_id
    )
ORDER BY
    ro.order_date DESC
