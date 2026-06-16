/*
Table ID: dim.Product

Description: |
    Conformed product dimension.

Primary key: ProductCode

Revisions:
    - 2026-06-16 Initial table.
*/

select
    p.ProductCode
  , p.ProductName
  , p.ProductCategory
from ExternalLake.catalog.ProductRaw as p
