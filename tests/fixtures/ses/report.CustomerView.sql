/*
View ID: report.CustomerView

Description: |
    Customer reporting view.

Revisions:
    - 2026-06-16 Initial view.

Column notes:
    CustomerCode: Stable customer key.
*/

select
    CustomerCode
from mart.Customer
