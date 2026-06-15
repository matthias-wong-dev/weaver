-- Order fulfillment pipeline extract.
-- Deliberately mixes temp tables, semicolon-free statements, and GO.
DECLARE @WarehouseId int = 42
DECLARE @RunId uniqueidentifier = NEWID()

SELECT
    o.OrderId,
    o.CustomerId,
    o.WarehouseId,
    o.PromiseDate,
    o.PriorityCode
INTO #OpenOrders
FROM sales.Orders AS o
WHERE
    o.WarehouseId = @WarehouseId
    AND o.FulfillmentStatus IN ('ALLOCATED', 'PICKING', 'PACKED')
    AND o.CancelledAt IS NULL

CREATE INDEX IX_OpenOrders_OrderId ON #OpenOrders (OrderId)

GO

IF EXISTS (
    SELECT
        1
    FROM #OpenOrders AS oo
    WHERE
        oo.PriorityCode = 'EXPRESS'
)
BEGIN
    PRINT 'Express orders detected for run';
END

INSERT INTO audit.FulfillmentRunLog (
    RunId,
    WarehouseId,
    OpenOrderCount,
    CreatedAt
)
SELECT
    @RunId,
    @WarehouseId,
    COUNT_BIG(*),
    SYSUTCDATETIME()
FROM #OpenOrders AS oo;

SELECT
    oo.OrderId,
    li.LineId,
    li.Sku,
    li.Quantity,
    inv.AvailableQuantity
FROM #OpenOrders AS oo
INNER JOIN sales.OrderLines AS li
    ON li.OrderId = oo.OrderId
LEFT JOIN warehouse.Inventory AS inv
    ON inv.Sku = li.Sku
    AND inv.WarehouseId = oo.WarehouseId
WHERE
    COALESCE(inv.AvailableQuantity, 0) < li.Quantity
ORDER BY
    oo.PromiseDate,
    oo.PriorityCode DESC;
