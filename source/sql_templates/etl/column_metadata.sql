;with source_columns as (
    select
        c.name
      , c.column_id
      , row_number() over (order by c.column_id) as row_ordinal
    from sys.columns as c
    where c.[object_id] = object_id($current_table_literal)
        and $source_column_filter
)
select
    @weaver_source_columns = string_agg(
        case when row_ordinal = 1 then quotename(name) else char(10) + N'      , ' + quotename(name) end,
        N''
    ) within group (order by column_id)
  , @weaver_staging_select_columns = string_agg(
        case when row_ordinal = 1 then N's.' + quotename(name) else char(10) + N'      , s.' + quotename(name) end,
        N''
    ) within group (order by column_id)
  , @weaver_staging_except_columns = string_agg(
        case when row_ordinal = 1 then N's.' + quotename(name) else char(10) + N'                  , s.' + quotename(name) end,
        N''
    ) within group (order by column_id)
  , @weaver_upsert_select_columns = string_agg(
        case when row_ordinal = 1 then N'u.' + quotename(name) else char(10) + N'      , u.' + quotename(name) end,
        N''
    ) within group (order by column_id)
  , @weaver_target_select_columns = string_agg(
        case when row_ordinal = 1 then N't.' + quotename(name) else char(10) + N'      , t.' + quotename(name) end,
        N''
    ) within group (order by column_id)
  , @weaver_target_except_columns = string_agg(
        case when row_ordinal = 1 then N't.' + quotename(name) else char(10) + N'                  , t.' + quotename(name) end,
        N''
    ) within group (order by column_id)
from source_columns;

;with history_columns as (
    select
        c.name
      , c.column_id
      , row_number() over (order by c.column_id) as row_ordinal
    from sys.columns as c
    where c.[object_id] = object_id($history_table_literal)
)
select
    @weaver_history_columns = string_agg(
        case when row_ordinal = 1 then quotename(name) else char(10) + N'          , ' + quotename(name) end,
        N''
    ) within group (order by column_id)
  , @weaver_history_select_columns = string_agg(
        case
            when name = N'Row delete datetime' and row_ordinal = 1 then N'@weaver_load_datetime as ' + quotename(name)
            when name = N'Row delete datetime' then char(10) + N'          , @weaver_load_datetime as ' + quotename(name)
            when row_ordinal = 1 then N'c.' + quotename(name)
            else char(10) + N'          , c.' + quotename(name)
        end,
        N''
    ) within group (order by column_id)
from history_columns;

$update_select

if @weaver_source_columns is null
begin
    throw 51002, 'weaver current table has no source columns to load.', 1;
end;

if @weaver_history_columns is null
begin
    throw 51003, 'weaver history table does not exist or has no columns.', 1;
end;
