/*
View ID: report.CustomerOrderSummary

Description: |
    Customer order summary reporting view.

Revisions:
    - 2026-06-16 Initial view.
*/

with order_rollup as (
    select
        fo.CustomerCode
      , count(*) as OrderCount
      , sum(fo.OrderAmountAud) as OrderAmountAud
    from fact.Order as fo
    group by
        fo.CustomerCode
)
select
    c.CustomerCode
  , c.CustomerName
  , r.OrderCount
  , r.OrderAmountAud
from dim.Customer as c
join order_rollup as r on r.CustomerCode = c.CustomerCode
where
    exists (
        select
            1
        from dim.Product as p
        where
            p.ProductCategory is not null
    )
