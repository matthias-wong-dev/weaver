/* Customer retention cohort report.
   Uses variables, CTEs, window functions, EXISTS predicates, and final ordering. */
DECLARE @StartDate date = '2026-01-01';
DECLARE @EndDate date = '2026-03-31';
DECLARE @MinimumOrders int = 2;

WITH base_customers AS (
    SELECT
        c.CustomerId,
        c.AccountNumber,
        c.CreatedAt,
        c.RegionCode
    FROM dbo.Customers AS c WITH (NOLOCK)
    WHERE
        c.CreatedAt < DATEADD(day, 1, @EndDate)
        AND c.IsTestAccount = 0
),
orders_in_window AS (
    SELECT
        o.CustomerId,
        COUNT_BIG(*) AS OrderCount,
        MIN(o.OrderDate) AS FirstOrderDate,
        MAX(o.OrderDate) AS LastOrderDate
    FROM sales.Orders AS o
    WHERE
        o.OrderDate >= @StartDate
        AND o.OrderDate < DATEADD(day, 1, @EndDate)
        AND o.StatusCode NOT IN ('CANCELLED', 'FRAUD')
    GROUP BY
        o.CustomerId
),
ranked_customers AS (
    SELECT
        bc.CustomerId,
        bc.AccountNumber,
        bc.RegionCode,
        oi.OrderCount,
        oi.FirstOrderDate,
        oi.LastOrderDate,
        ROW_NUMBER() OVER (
            PARTITION BY bc.RegionCode
            ORDER BY oi.OrderCount DESC, oi.LastOrderDate DESC
        ) AS RegionRank
    FROM base_customers AS bc
    INNER JOIN orders_in_window AS oi
        ON oi.CustomerId = bc.CustomerId
    WHERE
        oi.OrderCount >= @MinimumOrders
        AND EXISTS (
            SELECT
                1
            FROM crm.CustomerSubscriptions AS cs
            WHERE
                cs.CustomerId = bc.CustomerId
                AND cs.IsActive = 1
        )
)
SELECT
    rc.CustomerId,
    rc.AccountNumber,
    rc.RegionCode,
    rc.OrderCount,
    rc.FirstOrderDate,
    rc.LastOrderDate,
    rc.RegionRank
FROM ranked_customers AS rc
WHERE
    rc.RegionRank <= 100
ORDER BY
    rc.RegionCode,
    rc.RegionRank;
