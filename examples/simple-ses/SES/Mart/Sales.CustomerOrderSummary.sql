/*
View ID: Sales.CustomerOrderSummary

Description: |
    One row per customer with their order count and total order amount. A
    customer with no orders still appears, with zero counts. This is the
    Warehouse-facing reporting shape built on top of the Core Delta tables.

Lineage: Reads Core.Sales.Customer and Core.Sales.Order.
*/

with order_rollup as (
    select
            o.customer_id
        ,   count(*)        as order_count
        ,   sum(o.amount)   as total_amount
    from Core.Sales.Order as o
    group by
            o.customer_id
)
select
        c.customer_id
    ,   c.customer_name
    ,   c.segment
    ,   coalesce(r.order_count, 0)      as order_count
    ,   coalesce(r.total_amount, 0.00)  as total_amount
from      Core.Sales.Customer as c
left join order_rollup as r on r.customer_id = c.customer_id
