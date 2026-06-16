/*
Table ID: raw.Order

Description: |
    Raw order extract with customer validity checks.

Primary key: OrderNumber

Revisions:
    - 2026-06-16 Initial table.
*/

select
    o.OrderNumber
  , o.CustomerCode
  , o.ProductCode
  , o.OrderDate
  , o.OrderAmount
from ExternalLake.sales.OrderRaw as o
join raw.Customer              as c on c.CustomerCode = o.CustomerCode
