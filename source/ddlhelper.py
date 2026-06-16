"""Helpers for generating T-SQL DDL around query text."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import uuid

import yaml

from source.sqlwrangle import insert_select_into, insert_where_one_eq_zero
from source.sqlwrangle import insert_ctas


DEFAULT_TYPE_MAPPING_PATH = (
    Path(__file__).resolve().parents[1] / "config" / "warehouse_type_mapping.yml"
)


@dataclass(frozen=True)
class _TableNames:
    view_name: str
    current_table: str
    history_table: str
    staging_table: str
    upsert_table: str
    load_procedure: str
    current_pk_constraint: str


def wrap_create_or_alter_view(sql_text: str, view_name: str) -> str:
    """Wrap query text in a simple CREATE OR ALTER VIEW statement."""

    body = _normalise_view_body(sql_text)
    return f"create or alter view {_quote_multipart_identifier(view_name)} as\n{body}"


def generate_infer_create_table_sql(
    sql_text: str,
    target_table_name: str,
    *,
    identity_column: str | None = None,
    primary_key_columns: list[str] | None = None,
    temp_table_name: str | None = None,
    type_mapping_path: str | Path | None = None,
) -> str:
    """Generate a self-contained SQL script that infers and creates a table."""

    mapping = _load_type_mapping(type_mapping_path)
    temp_table_name = _normalise_temp_table_name(temp_table_name)
    guarded_sql = insert_where_one_eq_zero(sql_text)
    shape_sql = insert_select_into(guarded_sql, temp_table_name)
    shape_sql = _ensure_statement_terminated(shape_sql)

    create_sql = _render_infer_create_sql(
        temp_table_name=temp_table_name,
        target_table_name=target_table_name,
        identity_column=identity_column,
        primary_key_columns=primary_key_columns,
        mapping=mapping,
    )
    return (
        "/* weaver generated table-shape inference script. */\n"
        "set nocount on;\n\n"
        f"if object_id('tempdb..{temp_table_name}') is not null drop table {temp_table_name};\n\n"
        f"{shape_sql}\n\n"
        f"{create_sql}\n"
        f"\ndrop table {temp_table_name};\n"
    )


def build_create_table_sql_from_describe_rows(
    describe_rows: list[dict],
    target_table_name: str,
    *,
    identity_column: str | None = None,
    primary_key_columns: list[str] | None = None,
    type_mapping_path: str | Path | None = None,
) -> str:
    """Build Fabric table/view DDL from described result-set rows."""

    mapping = _load_type_mapping(type_mapping_path)
    table_names = _derive_table_names(target_table_name)
    primary_key_columns = _normalise_column_list(primary_key_columns)
    primary_key_lookup = {
        _normalise_identifier_name(column_name).lower()
        for column_name in primary_key_columns
    }
    visible_rows = [
        row
        for row in describe_rows
        if not row.get("is_hidden") and row.get("error_number") is None
    ]
    if not visible_rows:
        errors = [row for row in describe_rows if row.get("error_number") is not None]
        if errors:
            message = errors[0].get("error_message") or "describe failed"
            raise ValueError(f"Cannot describe result set: {message}")
        raise ValueError("Cannot create a table for a result set with no visible columns")

    names = _disambiguate_column_names(
        [
            str(row.get("name") or f"Column{row.get('column_ordinal')}")
            for row in visible_rows
        ]
    )

    current_column_definitions: list[str] = []
    history_column_definitions: list[str] = []
    view_columns: list[str] = []

    if identity_column:
        quoted_identity = _quote_identifier_part(identity_column)
        identity_type = mapping.get("identity_type", "bigint identity not null")
        current_column_definitions.append(f"{quoted_identity} {identity_type}")
        history_column_definitions.append(
            f"{quoted_identity} {_history_identity_type(identity_type)}"
        )
        view_columns.append(quoted_identity)

    for row, column_name in zip(visible_rows, names):
        warehouse_type = _translate_described_type(row, mapping)
        is_primary_key_column = column_name.lower() in primary_key_lookup
        nullability = "not null" if is_primary_key_column or not row.get("is_nullable") else "null"
        quoted_column = _quote_identifier_part(column_name)
        column_definition = f"{quoted_column} {warehouse_type} {nullability}"
        current_column_definitions.append(column_definition)
        history_column_definitions.append(column_definition)
        view_columns.append(quoted_column)

    current_column_definitions.extend(_current_row_datetime_definitions())
    history_column_definitions.extend(_history_row_datetime_definitions())
    view_columns.extend(_view_row_datetime_columns())

    return _render_backing_table_and_view_sql(
        table_names=table_names,
        current_column_definitions=current_column_definitions,
        history_column_definitions=history_column_definitions,
        view_columns=view_columns,
        primary_key_columns=primary_key_columns,
    )


def generate_load_stored_procedure_sql(
    sql_text: str,
    target_table_name: str,
    *,
    primary_key_columns: list[str] | None = None,
) -> str:
    """Generate SQL that creates a static ETL stored procedure."""

    table_names = _derive_table_names(target_table_name)
    primary_key_columns = _normalise_column_list(primary_key_columns)
    source_sql = _normalise_procedure_source_sql(sql_text)
    runtime_staging_sql = _ensure_statement_terminated(
        insert_ctas(source_sql, table_names.staging_table)
    )

    procedure_template = _render_static_load_procedure_template(
        table_names=table_names,
        runtime_staging_sql=runtime_staging_sql,
        primary_key_columns=primary_key_columns,
    )

    return (
        "/* weaver generated etl procedure installer. */\n"
        "set nocount on;\n\n"
        "if schema_id(N'_') is null exec(N'create schema [_]');\n\n"
        "declare @weaver_proc_sql nvarchar(max);\n"
        "declare @weaver_source_columns nvarchar(max);\n"
        "declare @weaver_staging_select_columns nvarchar(max);\n"
        "declare @weaver_staging_except_columns nvarchar(max);\n"
        "declare @weaver_upsert_select_columns nvarchar(max);\n"
        "declare @weaver_target_select_columns nvarchar(max);\n"
        "declare @weaver_target_except_columns nvarchar(max);\n"
        "declare @weaver_history_columns nvarchar(max);\n"
        "declare @weaver_history_select_columns nvarchar(max);\n"
        "declare @weaver_update_set_columns nvarchar(max);\n\n"
        f"{_render_installer_column_metadata_sql(table_names, primary_key_columns)}\n\n"
        f"set @weaver_proc_sql = {_sql_string_literal(procedure_template)};\n"
        "set @weaver_proc_sql = replace(@weaver_proc_sql, N'__SOURCE_COLUMNS__', @weaver_source_columns);\n"
        "set @weaver_proc_sql = replace(@weaver_proc_sql, N'__STAGING_SELECT_COLUMNS__', @weaver_staging_select_columns);\n"
        "set @weaver_proc_sql = replace(@weaver_proc_sql, N'__STAGING_EXCEPT_COLUMNS__', @weaver_staging_except_columns);\n"
        "set @weaver_proc_sql = replace(@weaver_proc_sql, N'__UPSERT_SELECT_COLUMNS__', @weaver_upsert_select_columns);\n"
        "set @weaver_proc_sql = replace(@weaver_proc_sql, N'__TARGET_SELECT_COLUMNS__', @weaver_target_select_columns);\n"
        "set @weaver_proc_sql = replace(@weaver_proc_sql, N'__TARGET_EXCEPT_COLUMNS__', @weaver_target_except_columns);\n"
        "set @weaver_proc_sql = replace(@weaver_proc_sql, N'__HISTORY_COLUMNS__', @weaver_history_columns);\n"
        "set @weaver_proc_sql = replace(@weaver_proc_sql, N'__HISTORY_SELECT_COLUMNS__', @weaver_history_select_columns);\n"
        "set @weaver_proc_sql = replace(@weaver_proc_sql, N'__UPDATE_SET_COLUMNS__', @weaver_update_set_columns);\n\n"
        "exec sys.sp_executesql @weaver_proc_sql;"
    )


def _render_static_load_procedure_template(
    *,
    table_names: _TableNames,
    runtime_staging_sql: str,
    primary_key_columns: list[str],
) -> str:
    if primary_key_columns:
        load_body = _render_static_primary_key_load_body(
            table_names=table_names,
            primary_key_columns=primary_key_columns,
        )
    else:
        load_body = _render_static_full_refresh_load_body(table_names=table_names)

    return (
        f"create or alter procedure {table_names.load_procedure}\n"
        "as\n"
        "begin\n"
        "    set nocount on;\n"
        "    declare @weaver_load_datetime datetime2(6) = sysutcdatetime();\n"
        "\n"
        f"    if object_id({_sql_string_literal(table_names.upsert_table)}, N'U') is not null drop table {table_names.upsert_table};\n"
        f"    if object_id({_sql_string_literal(table_names.staging_table)}, N'U') is not null drop table {table_names.staging_table};\n\n"
        f"{_indent_sql(runtime_staging_sql, 4)}\n\n"
        f"{_indent_sql(load_body, 4)}\n\n"
        f"    if object_id({_sql_string_literal(table_names.upsert_table)}, N'U') is not null drop table {table_names.upsert_table};\n"
        f"    if object_id({_sql_string_literal(table_names.staging_table)}, N'U') is not null drop table {table_names.staging_table};\n"
        "end;"
    )


def _normalise_view_body(sql_text: str) -> str:
    body = sql_text.strip()
    if body.endswith(";"):
        body = body[:-1].rstrip()

    if body[:1] == ";" and body[1:].lstrip().upper().startswith("WITH"):
        return body[1:].lstrip()
    return body


def _load_type_mapping(type_mapping_path: str | Path | None) -> dict:
    path = Path(type_mapping_path) if type_mapping_path else DEFAULT_TYPE_MAPPING_PATH
    with path.open("r", encoding="utf-8") as mapping_file:
        loaded = yaml.safe_load(mapping_file) or {}
    if "mappings" not in loaded:
        raise ValueError(f"Type mapping file {path} must define a mappings block")
    return loaded


def _normalise_temp_table_name(temp_table_name: str | None) -> str:
    if temp_table_name is None:
        return f"#weaver_shape_{uuid.uuid4().hex[:12]}"

    name = temp_table_name if temp_table_name.startswith("#") else f"#{temp_table_name}"
    if not re.fullmatch(r"#[A-Za-z_][A-Za-z0-9_]{0,110}", name):
        raise ValueError("temp_table_name must be a simple local temp table name")
    return name


def _ensure_statement_terminated(sql_text: str) -> str:
    stripped = sql_text.rstrip()
    return stripped if stripped.endswith(";") else f"{stripped};"


def _normalise_procedure_source_sql(sql_text: str) -> str:
    return sql_text.strip().rstrip(";").rstrip()


def _derive_table_names(target_table_name: str) -> _TableNames:
    parts = _split_identifier_parts(target_table_name)
    if len(parts) != 2:
        raise ValueError("target_table_name must be supplied as schema.table")

    schema_name, table_name = parts
    unquoted_table_name = _unquote_identifier_part(table_name)
    quoted_schema = _quote_identifier_part(schema_name)

    return _TableNames(
        view_name=f"{quoted_schema}.{_quote_identifier_part(unquoted_table_name)}",
        current_table=f"{quoted_schema}.{_quote_identifier_part(f'{unquoted_table_name}_Current')}",
        history_table=f"{quoted_schema}.{_quote_identifier_part(f'{unquoted_table_name}_History')}",
        staging_table=f"{quoted_schema}.{_quote_identifier_part(f'{unquoted_table_name}_Staging')}",
        upsert_table=f"{quoted_schema}.{_quote_identifier_part(f'{unquoted_table_name}_Upsert')}",
        load_procedure=f"[_].{_quote_identifier_part(f'ETL {schema_name}.{unquoted_table_name}')}",
        current_pk_constraint=_quote_identifier_part(f"PK_{unquoted_table_name}_Current"),
    )


def _unquote_identifier_part(part: str) -> str:
    stripped = part.strip()
    if stripped.startswith("[") and stripped.endswith("]"):
        return stripped[1:-1].replace("]]", "]")
    return stripped


def _history_identity_type(identity_type: str) -> str:
    return re.sub(
        r"\s+IDENTITY(?:\s*\([^)]*\))?",
        "",
        identity_type,
        flags=re.IGNORECASE,
    ).strip()


def _normalise_column_list(columns: list[str] | None) -> list[str]:
    if columns is None:
        return []
    if isinstance(columns, str):
        raise TypeError("columns must be supplied as a list of column names")

    normalised = [_normalise_identifier_name(column) for column in columns]
    if any(not column for column in normalised):
        raise ValueError("column names must not be empty")
    return normalised


def _normalise_identifier_name(identifier: str) -> str:
    parts = _split_identifier_parts(str(identifier))
    if len(parts) != 1:
        raise ValueError("column names must not be multipart identifiers")
    return _unquote_identifier_part(parts[0])


def _current_row_datetime_definitions() -> list[str]:
    return [
        "[Row insert datetime] datetime2(6) null",
        "[Row update datetime] datetime2(6) null",
        "[Row delete datetime] datetime2(6) not null",
    ]


def _history_row_datetime_definitions() -> list[str]:
    return [
        "[Row insert datetime] datetime2(6) null",
        "[Row update datetime] datetime2(6) null",
        "[Row delete datetime] datetime2(6) not null",
    ]


def _view_row_datetime_columns() -> list[str]:
    return ["[Row insert datetime]", "[Row update datetime]"]


def _render_backing_table_and_view_sql(
    *,
    table_names: _TableNames,
    current_column_definitions: list[str],
    history_column_definitions: list[str],
    view_columns: list[str],
    primary_key_columns: list[str],
) -> str:
    joined_current_columns = _join_leading_comma_list(current_column_definitions)
    joined_history_columns = _join_leading_comma_list(history_column_definitions)
    joined_view_columns = _join_leading_comma_list(view_columns)
    primary_key_sql = _render_primary_key_sql(
        table_names=table_names,
        primary_key_columns=primary_key_columns,
    )
    post_create_sql = "\n\n".join(
        statement for statement in [primary_key_sql] if statement
    )
    post_create_section = f"\n\n{post_create_sql}" if post_create_sql else ""

    return (
        f"create table {table_names.current_table} (\n"
        f"{joined_current_columns}\n"
        ");\n\n"
        f"create table {table_names.history_table} (\n"
        f"{joined_history_columns}\n"
        ");"
        f"{post_create_section}\n\n"
        f"create or alter view {table_names.view_name} as\n"
        "select\n"
        f"{joined_view_columns}\n"
        f"from {table_names.current_table};"
    )


def _join_leading_comma_list(
    items: list[str],
    *,
    first_indent: str = "    ",
    comma_indent: str = "  ",
) -> str:
    if not items:
        return ""
    lines = [f"{first_indent}{items[0]}"]
    lines.extend(f"{comma_indent}, {item}" for item in items[1:])
    return "\n".join(lines)


def _render_primary_key_sql(
    *,
    table_names: _TableNames,
    primary_key_columns: list[str],
) -> str:
    if not primary_key_columns:
        return ""

    joined_key_columns = ", ".join(
        _quote_identifier_part(column_name) for column_name in primary_key_columns
    )
    return (
        f"alter table {table_names.current_table} "
        f"add constraint {table_names.current_pk_constraint} "
        f"primary key nonclustered ({joined_key_columns}) not enforced;"
    )


def _render_primary_key_columns_cte(primary_key_columns: list[str]) -> str:
    if not primary_key_columns:
        return (
            "    select\n"
            "        convert(int, null) as column_ordinal\n"
            "      , convert(nvarchar(128), null) as column_name\n"
            "    where 1 = 0"
        )

    values = _join_leading_comma_list(
        [
            f"({index}, {_sql_string_literal(column_name)})"
            for index, column_name in enumerate(primary_key_columns, start=1)
        ],
        first_indent="        ",
        comma_indent="      ",
    )
    return (
        "    select\n"
        "        column_ordinal\n"
        "      , column_name\n"
        "    from (values\n"
        f"{values}\n"
        "    ) as pk(column_ordinal, column_name)"
    )


def _render_installer_column_metadata_sql(
    table_names: _TableNames,
    primary_key_columns: list[str],
) -> str:
    primary_key_values = _render_primary_key_name_values(primary_key_columns)
    source_column_filter = (
        "c.name not in (N'Row insert datetime', N'Row update datetime', N'Row delete datetime')\n"
        "        and c.is_identity = 0"
    )
    primary_key_filter = (
        f"and lower(c.name) not in (\n{primary_key_values}\n        )"
        if primary_key_columns
        else ""
    )
    update_select = (
        ";with update_columns as (\n"
        "    select\n"
        "        c.name\n"
        "      , c.column_id\n"
        "      , row_number() over (order by c.column_id) as row_ordinal\n"
        "    from sys.columns as c\n"
        f"    where c.[object_id] = object_id({_sql_string_literal(table_names.current_table)})\n"
        f"        and {source_column_filter}\n"
        f"        {primary_key_filter}\n"
        ")\n"
        "select\n"
        "    @weaver_update_set_columns =\n"
        "        coalesce(\n"
        "            string_agg(\n"
        "                case\n"
        "                    when row_ordinal = 1 then N'c.' + quotename(name) + N' = u.' + quotename(name)\n"
        "                    else char(10) + N'          , c.' + quotename(name) + N' = u.' + quotename(name)\n"
        "                end,\n"
        "                N''\n"
        "            ) within group (order by column_id)\n"
        "            + char(10) + N'          , ',\n"
        "            N''\n"
        "        )\n"
        "        + N'c.[Row update datetime] = @weaver_load_datetime'\n"
        "        + char(10) + N'          , c.[Row delete datetime] = convert(datetime2(6), ''9999-12-31 00:00:00'')'\n"
        "from update_columns;"
        if primary_key_columns
        else "set @weaver_update_set_columns = N'';"
    )

    return f""";with source_columns as (
    select
        c.name
      , c.column_id
      , row_number() over (order by c.column_id) as row_ordinal
    from sys.columns as c
    where c.[object_id] = object_id({_sql_string_literal(table_names.current_table)})
        and {source_column_filter}
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
    where c.[object_id] = object_id({_sql_string_literal(table_names.history_table)})
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

{update_select}

if @weaver_source_columns is null
begin
    throw 51002, 'weaver current table has no source columns to load.', 1;
end;

if @weaver_history_columns is null
begin
    throw 51003, 'weaver history table does not exist or has no columns.', 1;
end;"""


def _render_static_full_refresh_load_body(*, table_names: _TableNames) -> str:
    return f"""delete from {table_names.current_table};

insert into {table_names.current_table} (
    __SOURCE_COLUMNS__
  , [Row insert datetime]
  , [Row update datetime]
  , [Row delete datetime]
)
select
    __STAGING_SELECT_COLUMNS__
  , @weaver_load_datetime
  , @weaver_load_datetime
  , convert(datetime2(6), '9999-12-31 00:00:00')
from {table_names.staging_table} as s;"""


def _render_static_primary_key_load_body(
    *,
    table_names: _TableNames,
    primary_key_columns: list[str],
) -> str:
    staging_target_join = _pk_join_predicate("s", "t", primary_key_columns)
    current_upsert_join = _pk_join_predicate("c", "u", primary_key_columns)
    history_upsert_join = _pk_join_predicate("h", "u", primary_key_columns)
    staging_current_join = _pk_join_predicate("s", "c", primary_key_columns)
    staging_history_join = _pk_join_predicate("s", "h", primary_key_columns)
    target_missing_predicate = f"t.{_quote_identifier_part(primary_key_columns[0])} is null"
    delete_missing_filter = (
        f"not exists (select 1 from {table_names.staging_table} as s "
        f"where {staging_current_join})"
    )
    delete_history_unwind_filter = (
        f"not exists (select 1 from {table_names.staging_table} as s "
        f"where {staging_history_join})"
    )

    return f"""create table {table_names.upsert_table} as
select
    __STAGING_SELECT_COLUMNS__
  , case when {target_missing_predicate} then cast(1 as int) else cast(0 as int) end as [_Is new row]
from {table_names.staging_table} as s
left join {table_names.view_name} as t on {staging_target_join}
where
    (
        {target_missing_predicate}
        or exists (
            select
                __STAGING_EXCEPT_COLUMNS__
            except
            select
                __TARGET_EXCEPT_COLUMNS__
        )
    );

insert into {table_names.current_table} (
    __SOURCE_COLUMNS__
  , [Row insert datetime]
  , [Row update datetime]
  , [Row delete datetime]
)
select
    __UPSERT_SELECT_COLUMNS__
  , @weaver_load_datetime
  , @weaver_load_datetime
  , convert(datetime2(6), '9999-12-31 00:00:00')
from {table_names.upsert_table} as u
where u.[_Is new row] = 1;

begin try
    insert into {table_names.history_table} (
        __HISTORY_COLUMNS__
    )
    select
        __HISTORY_SELECT_COLUMNS__
    from {table_names.current_table} as c
    inner join {table_names.upsert_table} as u on {current_upsert_join}
    where u.[_Is new row] = 0;

    update c
    set
        __UPDATE_SET_COLUMNS__
    from {table_names.current_table} as c
    inner join {table_names.upsert_table} as u on {current_upsert_join}
    where u.[_Is new row] = 0;
end try
begin catch
    delete h
    from {table_names.history_table} as h
    inner join {table_names.upsert_table} as u on {history_upsert_join}
    where u.[_Is new row] = 0
        and h.[Row delete datetime] = @weaver_load_datetime;

    throw;
end catch;

begin try
    insert into {table_names.history_table} (
        __HISTORY_COLUMNS__
    )
    select
        __HISTORY_SELECT_COLUMNS__
    from {table_names.current_table} as c
    where {delete_missing_filter};

    delete c
    from {table_names.current_table} as c
    where {delete_missing_filter};
end try
begin catch
    delete h
    from {table_names.history_table} as h
    where h.[Row delete datetime] = @weaver_load_datetime
        and {delete_history_unwind_filter};

    throw;
end catch;"""


def _render_primary_key_name_values(primary_key_columns: list[str]) -> str:
    return _join_leading_comma_list(
        [_sql_string_literal(column_name.lower()) for column_name in primary_key_columns],
        first_indent="            ",
        comma_indent="          ",
    )


def _pk_join_predicate(left_alias: str, right_alias: str, columns: list[str]) -> str:
    predicates = [
        f"{left_alias}.{_quote_identifier_part(column)} = {right_alias}.{_quote_identifier_part(column)}"
        for column in columns
    ]
    if not predicates:
        return ""
    return "\n    and ".join(predicates)


def _indent_sql(sql_text: str, spaces: int) -> str:
    prefix = " " * spaces
    return "\n".join(f"{prefix}{line}" if line else "" for line in sql_text.splitlines())


def _render_infer_create_sql(
    *,
    temp_table_name: str,
    target_table_name: str,
    identity_column: str | None,
    primary_key_columns: list[str] | None,
    mapping: dict,
) -> str:
    table_names = _derive_table_names(target_table_name)
    primary_key_columns = _normalise_column_list(primary_key_columns)
    identity_type = mapping.get("identity_type", "bigint identity not null")
    history_identity_type = _history_identity_type(identity_type)
    identity_literal = (
        "null" if identity_column is None else _sql_string_literal(identity_column)
    )
    type_case = _render_type_mapping_case(mapping)
    temp_object_literal = _sql_string_literal(f"tempdb..{temp_table_name}")
    primary_key_columns_cte = _render_primary_key_columns_cte(primary_key_columns)

    return f"""declare @weaver_identity_column varchar(128) = {identity_literal};
declare @weaver_current_create_sql nvarchar(max);
declare @weaver_history_create_sql nvarchar(max);
declare @weaver_current_pk_sql nvarchar(max);
declare @weaver_view_sql nvarchar(max);

if not exists (
    select 1
    from tempdb.sys.columns as c
    where c.[object_id] = object_id({temp_object_literal})
)
begin
    throw 51001, 'weaver found no temp table columns to create.', 1;
end;

;with primary_key_columns as (
{primary_key_columns_cte}
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
    where c.[object_id] = object_id({temp_object_literal})
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
        {type_case} as warehouse_type,
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
        quotename(@weaver_identity_column) + N' {identity_type}' as current_column_definition,
        quotename(@weaver_identity_column) + N' {history_identity_type}' as history_column_definition,
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
            N'create table {table_names.current_table} (' + char(10)
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
            N'create table {table_names.history_table} (' + char(10)
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
            N'create or alter view {table_names.view_name} as' + char(10)
            + N'select' + char(10)
            + string_agg(
                case
                    when column_ordinal = 1 then N'    ' + view_column
                    else N'  , ' + view_column
                end,
                char(10)
            ) within group (order by column_ordinal)
            + char(10) + N'from {table_names.current_table};'
        from all_columns
        where view_column is not null
    ),
    @weaver_current_pk_sql = (
        select
            N'alter table {table_names.current_table} add constraint {table_names.current_pk_constraint} '
            + N'primary key nonclustered ('
            + string_agg(quotename(column_name), N', ') within group (order by column_ordinal)
            + N') not enforced;'
        from primary_key_columns
    );

print @weaver_current_create_sql;
exec sys.sp_executesql @weaver_current_create_sql;

print @weaver_history_create_sql;
exec sys.sp_executesql @weaver_history_create_sql;

if @weaver_current_pk_sql is not null
begin
    print @weaver_current_pk_sql;
    exec sys.sp_executesql @weaver_current_pk_sql;
end;

print @weaver_view_sql;
exec sys.sp_executesql @weaver_view_sql;"""


def _disambiguate_column_names(column_names: list[str]) -> list[str]:
    totals = {name: column_names.count(name) for name in column_names}
    seen: dict[str, int] = {}
    result: list[str] = []

    for name in column_names:
        seen[name] = seen.get(name, 0) + 1
        if totals[name] == 1:
            result.append(name)
        else:
            result.append(f"{name}_{seen[name]}")
    return result


def _translate_described_type(row: dict, mapping: dict) -> str:
    base_type = _base_type_name(str(row.get("system_type_name") or ""))
    rule = mapping.get("mappings", {}).get(base_type)
    if rule is None:
        return mapping.get("fallback_type", "varchar(max)")

    target = rule["target"]
    if "precision" in rule and "scale" in rule:
        precision = _metadata_numeric_part(row, rule["precision"], "precision")
        scale = _metadata_numeric_part(row, rule["scale"], "scale")
        return f"{target}({precision},{scale})"
    if "scale" in rule:
        return f"{target}({_metadata_scale(row, rule['scale'])})"
    if "length" in rule:
        return f"{target}({_metadata_length(row, base_type, rule['length'])})"
    return target


def _base_type_name(system_type_name: str) -> str:
    return system_type_name.split("(", maxsplit=1)[0].strip().lower()


def _metadata_numeric_part(row: dict, value: str | int, field: str) -> int:
    if value == "source":
        default = 38 if field == "precision" else 0
        return int(row.get(field) or default)
    return int(value)


def _metadata_scale(row: dict, value: str | int) -> int:
    if value == "min_source_6":
        scale = int(row.get("scale") if row.get("scale") is not None else 6)
        return min(max(scale, 0), 6)
    return int(value)


def _metadata_length(row: dict, base_type: str, value: str | int) -> str:
    if value == "max":
        return "max"
    if value != "source":
        return str(value)

    max_length = row.get("max_length")
    if max_length is None or int(max_length) == 0:
        return "1"
    if int(max_length) == -1:
        return "max"

    divisor = 2 if base_type in {"nchar", "nvarchar"} else 1
    length = max(int(max_length) // divisor, 1)
    return str(length)


def _render_type_mapping_case(mapping: dict) -> str:
    fallback = mapping.get("fallback_type", "varchar(max)")
    mappings = mapping.get("mappings", {})
    lines = ["case bt.base_type"]
    for source_type in sorted(mappings):
        expression = _render_target_type_expression(source_type, mappings[source_type])
        lines.append(f"            when '{source_type.lower()}' then {expression}")
    lines.append(f"            else N'{_escape_sql_literal(fallback)}'")
    lines.append("        end")
    return "\n        ".join(lines)


def _render_target_type_expression(source_type: str, mapping: dict) -> str:
    target = mapping["target"]
    if "precision" in mapping and "scale" in mapping:
        precision = _numeric_type_part_expression(mapping["precision"], "precision")
        scale = _numeric_type_part_expression(mapping["scale"], "scale")
        return f"N'{target}(' + {precision} + N',' + {scale} + N')'"

    if "scale" in mapping:
        scale = _scale_expression(mapping["scale"])
        return f"N'{target}(' + {scale} + N')'"

    if "length" in mapping:
        length = _length_expression(source_type, mapping["length"])
        return f"N'{target}(' + {length} + N')'"

    return f"N'{target}'"


def _numeric_type_part_expression(value: str | int, column_name: str) -> str:
    if value == "source":
        default_value = "38" if column_name == "precision" else "0"
        return (
            f"convert(nvarchar(20), "
            f"coalesce(nullif(convert(int, d.{column_name}), 0), {default_value}))"
        )
    return f"N'{value}'"


def _scale_expression(value: str | int) -> str:
    if value == "min_source_6":
        return (
            "convert(nvarchar(20), "
            "case "
            "when d.scale is null then 6 "
            "when convert(int, d.scale) > 6 then 6 "
            "when convert(int, d.scale) < 0 then 0 "
            "else convert(int, d.scale) "
            "end)"
        )
    return f"N'{value}'"


def _length_expression(source_type: str, value: str | int) -> str:
    if value == "max":
        return "N'max'"
    if value == "source":
        divisor = "2" if source_type.lower() in {"nchar", "nvarchar"} else "1"
        source_length = f"convert(int, d.max_length) / {divisor}"
        return (
            "case "
            "when d.max_length = -1 then N'max' "
            "when d.max_length is null or d.max_length = 0 then N'1' "
            f"else convert(nvarchar(20), case when {source_length} < 1 then 1 else {source_length} end) "
            "end"
        )
    return f"N'{value}'"


def _quote_multipart_identifier(identifier: str) -> str:
    parts = _split_identifier_parts(identifier)
    if not parts:
        raise ValueError("identifier must not be empty")
    return ".".join(_quote_identifier_part(part) for part in parts)


def _split_identifier_parts(identifier: str) -> list[str]:
    parts: list[str] = []
    current: list[str] = []
    in_brackets = False

    for character in identifier.strip():
        if character == "[" and not in_brackets:
            in_brackets = True
            current.append(character)
            continue
        if character == "]" and in_brackets:
            in_brackets = False
            current.append(character)
            continue
        if character == "." and not in_brackets:
            part = "".join(current).strip()
            if part:
                parts.append(part)
            current = []
            continue
        current.append(character)

    part = "".join(current).strip()
    if part:
        parts.append(part)
    return parts


def _quote_identifier_part(part: str) -> str:
    stripped = part.strip()
    if stripped.startswith("[") and stripped.endswith("]"):
        stripped = stripped[1:-1]
    return f"[{stripped.replace(']', ']]')}]"


def _sql_string_literal(value: str) -> str:
    return f"N'{_escape_sql_literal(value)}'"


def _escape_sql_literal(value: str) -> str:
    return value.replace("'", "''")
