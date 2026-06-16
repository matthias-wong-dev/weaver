SELECT
    'ACTIVE_PRODUCT' AS row_kind,
    p.sku,
    p.category_code,
    p.unit_price,
    CAST(0 AS int) AS units_sold
FROM dbo.weaver_fixture_products AS p
WHERE
    p.is_active = 1
UNION ALL
SELECT
    'SOLD_PRODUCT' AS row_kind,
    p.sku,
    p.category_code,
    p.unit_price,
    SUM(ol.quantity) AS units_sold
FROM dbo.weaver_fixture_products AS p
INNER JOIN dbo.weaver_fixture_order_lines AS ol
    ON ol.product_id = p.product_id
GROUP BY
    p.sku,
    p.category_code,
    p.unit_price
ORDER BY
    row_kind,
    sku
