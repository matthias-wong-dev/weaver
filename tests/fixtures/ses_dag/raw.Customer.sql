/*
Table ID: raw.Customer

Description: |
    Raw customer extract.

Primary key: CustomerCode

Revisions:
    - 2026-06-16 Initial table.
*/

select
    CustomerCode
  , CustomerName
  , CustomerSegment
from ExternalLake.crm.CustomerRaw
