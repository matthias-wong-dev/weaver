set nocount on;

declare @weaver_sql nvarchar(max);

select
    @weaver_sql = string_agg(
        convert(
            nvarchar(max),
            N'alter table '
            + quotename(object_schema_name(fk.parent_object_id))
            + N'.'
            + quotename(object_name(fk.parent_object_id))
            + N' drop constraint '
            + quotename(fk.name)
            + N';'
        ),
        char(10)
    )
from sys.foreign_keys as fk
where schema_name(schema_id) not in (N'dbo', N'guest', N'information_schema', N'sys');

if @weaver_sql is not null
begin
    exec sys.sp_executesql @weaver_sql;
end;

select
    @weaver_sql = string_agg(
        convert(
            nvarchar(max),
            N'drop view '
            + quotename(schema_name(schema_id))
            + N'.'
            + quotename(name)
            + N';'
        ),
        char(10)
    )
from sys.views
where schema_name(schema_id) not in (N'dbo', N'guest', N'information_schema', N'sys');

if @weaver_sql is not null
begin
    exec sys.sp_executesql @weaver_sql;
end;

select
    @weaver_sql = string_agg(
        convert(
            nvarchar(max),
            N'drop table '
            + quotename(schema_name(schema_id))
            + N'.'
            + quotename(name)
            + N';'
        ),
        char(10)
    )
from sys.tables
where schema_name(schema_id) not in (N'dbo', N'guest', N'information_schema', N'sys');

if @weaver_sql is not null
begin
    exec sys.sp_executesql @weaver_sql;
end;

select
    @weaver_sql = string_agg(
        convert(
            nvarchar(max),
            N'drop schema '
            + quotename(name)
            + N';'
        ),
        char(10)
    )
from sys.schemas
where name not in (N'dbo', N'guest', N'information_schema', N'sys')
    and schema_id < 16384;

if @weaver_sql is not null
begin
    exec sys.sp_executesql @weaver_sql;
end;
