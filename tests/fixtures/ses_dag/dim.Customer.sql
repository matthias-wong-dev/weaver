/*
Table ID: dim.Customer

Description: |
    Conformed customer dimension.

Primary key: CustomerCode

Revisions:
    - 2026-06-16 Initial table.
*/

with active_customer as (
    select
        c.CustomerCode
      , c.CustomerName
      , c.CustomerSegment
    from [raw].[Customer] as c
    where
        c.CustomerName is not null
)
select
    ac.CustomerCode
  , ac.CustomerName
  , ac.CustomerSegment
from active_customer as ac
