/*
View ID: audit.CustomerOrderQuality

Description: |
    Data quality checks over customer order outputs.

Revisions:
    - 2026-06-16 Initial view.
*/

select
    s.CustomerCode
  , s.OrderCount
  , case
        when f.OrderNumber is null then 'missing fact row'
        when s.OrderCount < 0 then 'invalid count'
        else 'ok'
    end as QualityStatus
from report.CustomerOrderSummary as s
left join fact.Order             as f on f.CustomerCode = s.CustomerCode
where
    (
        f.OrderNumber is null
        or s.OrderCount < 0
    )
