DECLARE @region_code varchar(10) = NULL;

SELECT
    c.customer_id,
    c.customer_name,
    c.region_code,
    COUNT(DISTINCT o.order_id) AS order_count,
    SUM(CAST(ol.quantity AS decimal(18,2)) * p.unit_price) AS gross_amount,
    MAX(o.order_date) AS last_order_date
FROM dbo.weaver_fixture_customers AS c
LEFT JOIN dbo.weaver_fixture_orders AS o
    ON o.customer_id = c.customer_id
LEFT JOIN dbo.weaver_fixture_order_lines AS ol
    ON ol.order_id = o.order_id
LEFT JOIN dbo.weaver_fixture_products AS p
    ON p.product_id = ol.product_id
WHERE
    (@region_code IS NULL OR c.region_code = @region_code)
    AND c.is_active = 1
GROUP BY
    c.customer_id,
    c.customer_name,
    c.region_code
ORDER BY
    gross_amount DESC
