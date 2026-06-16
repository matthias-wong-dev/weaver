/*
Table ID: mart.Customer

Description: |
    Customer dimension.

Primary key: CustomerCode

Unique keys:
    - CustomerName, CustomerSegment

Identity: Customer SK

Revisions:
    - 2026-06-16 Initial table.

Column notes:
    CustomerCode: Source customer code.
    CustomerName: Source customer display name.
*/

select
    CustomerCode
  , CustomerName
  , CustomerSegment
from ExternalLake.dbo.SourceCustomers
