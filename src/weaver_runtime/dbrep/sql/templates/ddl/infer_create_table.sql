declare @weaver_identity_column varchar(128) = $identity_literal;
declare @weaver_current_create_sql nvarchar(max);
declare @weaver_history_create_sql nvarchar(max);
declare @weaver_current_pk_sql nvarchar(max);
declare @weaver_view_sql nvarchar(max);

if not exists (
    select 1
    from tempdb.sys.columns as c
    where c.[object_id] = object_id($temp_object_literal)
)
begin
    throw 51001, 'weaver found no temp table columns to create.', 1;
end;

$metadata_validation_sql

;with primary_key_columns as (
$primary_key_columns_cte
),
raw_described as (
    select
        c.column_id as column_ordinal,
        coalesce(nullif(c.name, ''), concat('Column', c.column_id)) as column_name,
        t.name as system_type_name,
        c.max_length,
        c.precision,
        c.scale,
        c.is_nullable
    from tempdb.sys.columns as c
    inner join tempdb.sys.types as t on t.user_type_id = c.user_type_id
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
        end as column_name,
        system_type_name,
        max_length,
        precision,
        scale,
        is_nullable
    from raw_described
),
mapped as (
    select
        d.column_ordinal,
        quotename(d.column_name) as quoted_column_name,
        $type_case as warehouse_type,
        case
            when pk.column_name is not null or d.is_nullable = 0 then N' not null'
            else N' null'
        end as nullability
    from described as d
    left join primary_key_columns as pk on lower(pk.column_name) = lower(d.column_name)
    cross apply (
        select
            lower(d.system_type_name) as base_type
    ) as bt
),
source_column_definitions as (
    select
        column_ordinal + case when @weaver_identity_column is null then 0 else 1 end as column_ordinal,
        quoted_column_name,
        quoted_column_name + N' ' + warehouse_type + nullability as current_column_definition,
        quoted_column_name + N' ' + warehouse_type + nullability as history_column_definition,
        quoted_column_name as view_column
    from mapped
),
all_columns as (
    select
        1 as column_ordinal,
        quotename(@weaver_identity_column) as quoted_column_name,
        quotename(@weaver_identity_column) + N' $identity_type' as current_column_definition,
        quotename(@weaver_identity_column) + N' $history_identity_type' as history_column_definition,
        quotename(@weaver_identity_column) as view_column
    where @weaver_identity_column is not null

    union all

    select
        column_ordinal,
        quoted_column_name,
        current_column_definition,
        history_column_definition,
        view_column
    from source_column_definitions

    union all

    select
        1000001,
        N'[Row insert datetime]',
        N'[Row insert datetime] datetime2(6) null',
        N'[Row insert datetime] datetime2(6) null',
        N'[Row insert datetime]'

    union all

    select
        1000002,
        N'[Row update datetime]',
        N'[Row update datetime] datetime2(6) null',
        N'[Row update datetime] datetime2(6) null',
        N'[Row update datetime]'

    union all

    select
        1000003,
        N'[Row delete datetime]',
        N'[Row delete datetime] datetime2(6) not null',
        N'[Row delete datetime] datetime2(6) not null',
        null
)
select
    @weaver_current_create_sql = (
        select
            N'create table $current_table (' + char(10)
            + string_agg(
                case
                    when column_ordinal = 1 then N'    ' + current_column_definition
                    else N'  , ' + current_column_definition
                end,
                char(10)
            ) within group (order by column_ordinal)
            + char(10) + N');'
        from all_columns
    ),
    @weaver_history_create_sql = (
        select
            N'create table $history_table (' + char(10)
            + string_agg(
                case
                    when column_ordinal = 1 then N'    ' + history_column_definition
                    else N'  , ' + history_column_definition
                end,
                char(10)
            ) within group (order by column_ordinal)
            + char(10) + N');'
        from all_columns
    ),
    @weaver_view_sql = (
        select
            N'create or alter view $view_name as' + char(10)
            + N'select' + char(10)
            + string_agg(
                case
                    when column_ordinal = 1 then N'    ' + view_column
                    else N'  , ' + view_column
                end,
                char(10)
            ) within group (order by column_ordinal)
            + char(10) + N'from $current_table;'
        from all_columns
        where view_column is not null
    ),
    @weaver_current_pk_sql = (
        select
            N'alter table $current_table add constraint $current_pk_constraint '
            + N'primary key nonclustered ('
            + string_agg(quotename(column_name), N', ') within group (order by column_ordinal)
            + N') not enforced;'
        from primary_key_columns
    );

if object_id($current_table_literal, N'U') is null
begin
    print @weaver_current_create_sql;
    exec sys.sp_executesql @weaver_current_create_sql;
end;

if object_id($history_table_literal, N'U') is null
begin
    print @weaver_history_create_sql;
    exec sys.sp_executesql @weaver_history_create_sql;
end;

if @weaver_current_pk_sql is not null
    and not exists (
        select 1
        from sys.key_constraints
        where parent_object_id = object_id($current_table_literal)
            and type = 'PK'
    )
begin
    print @weaver_current_pk_sql;
    exec sys.sp_executesql @weaver_current_pk_sql;
end;

print @weaver_view_sql;
exec sys.sp_executesql @weaver_view_sql;
