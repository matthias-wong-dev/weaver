declare @weaver_missing_metadata_column nvarchar(2048);

;with raw_described as (
    select
        c.column_id as column_ordinal,
        coalesce(nullif(c.name, ''), concat('Column', c.column_id)) as column_name
    from tempdb.sys.columns as c
    where c.[object_id] = object_id($temp_object_literal)
),
described as (
    select
        column_ordinal,
        case
            when count(*) over (partition by column_name) = 1 then column_name
            else concat(
                column_name,
                '_',
                row_number() over (partition by column_name order by column_ordinal)
            )
        end as column_name
    from raw_described
),
generated_columns as (
    select
        column_name
    from described

    union all

    select
        @weaver_identity_column
    where @weaver_identity_column is not null
),
metadata_columns as (
$metadata_columns_cte
)
select top (1)
    @weaver_missing_metadata_column =
        concat(metadata_kind, N' ', column_name, N' does not exist')
from metadata_columns as m
where not exists (
    select 1
    from generated_columns as gc
    where lower(gc.column_name) = lower(m.column_name)
)
order by
    metadata_kind
  , column_name;

if @weaver_missing_metadata_column is not null
begin
    throw 51004, @weaver_missing_metadata_column, 1;
end;
