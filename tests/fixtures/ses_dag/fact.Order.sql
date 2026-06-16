/*
Table ID: fact.Order

Description: |
    Order fact table.

Primary key: OrderNumber

Revisions:
    - 2026-06-16 Initial table.
*/

select
    o.OrderNumber
  , c.CustomerCode
  , p.ProductCode
  , o.OrderDate
  , cast(o.OrderAmount * fx.RateToAud as decimal(18, 2)) as OrderAmountAud
from raw.Order             as o
join dim.Customer          as c on c.CustomerCode = o.CustomerCode
join dim.Product           as p on p.ProductCode = o.ProductCode
left join FinanceDb.ref.Fx as fx on fx.RateDate = o.OrderDate
    and fx.FromCurrency = 'USD'
    and fx.ToCurrency = 'AUD'
