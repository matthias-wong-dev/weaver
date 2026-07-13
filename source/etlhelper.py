"""Helpers for generating ETL stored procedure SQL."""

from __future__ import annotations

from source.ddlhelper import (
    _derive_table_names,
    _ensure_statement_terminated,
    _indent_sql,
    _join_leading_comma_list,
    _normalise_column_list,
    _normalise_procedure_source_sql,
    _quote_identifier_part,
    _sql_string_literal,
)
from source.sqlwrangle import insert_ctas, render_sql_template


def generate_load_stored_procedure_sql(
    sql_text: str,
    target_table_name: str,
    *,
    primary_key_columns: list[str] | None = None,
    is_incremental: bool = False,
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
        is_incremental=is_incremental,
    )

    return render_sql_template(
        "etl/create_etl_proc_installer",
        column_metadata_sql=_render_installer_column_metadata_sql(
            table_names,
            primary_key_columns,
        ),
        procedure_template_sql_literal=_sql_string_literal(procedure_template),
    )


def _render_static_load_procedure_template(
    *,
    table_names,
    runtime_staging_sql: str,
    primary_key_columns: list[str],
    is_incremental: bool,
) -> str:
    has_primary_key = bool(primary_key_columns)
    if primary_key_columns:
        load_body = _render_static_primary_key_load_body(
            table_names=table_names,
            primary_key_columns=primary_key_columns,
            is_incremental=is_incremental,
        )
    else:
        load_body = _render_static_full_refresh_load_body(table_names=table_names)

    return render_sql_template(
        "etl/load_proc",
        load_procedure=table_names.load_procedure,
        start_artifact_cleanup=_indent_sql(
            _render_start_artifact_cleanup(table_names),
            4,
        ),
        runtime_staging_sql=_indent_sql(runtime_staging_sql, 4),
        load_body=_indent_sql(load_body, 4),
        end_artifact_cleanup=_indent_sql(
            _render_end_artifact_cleanup(table_names, has_primary_key),
            4,
        ),
    ).rstrip()


def _render_start_artifact_cleanup(table_names) -> str:
    return (
        f"if object_id({_sql_string_literal(table_names.reject_table)}, N'U') is not null drop table {table_names.reject_table};\n"
        f"if object_id({_sql_string_literal(table_names.upsert_table)}, N'U') is not null drop table {table_names.upsert_table};\n"
        f"if object_id({_sql_string_literal(table_names.accepted_table)}, N'U') is not null drop table {table_names.accepted_table};\n"
        f"if object_id({_sql_string_literal(table_names.staging_table)}, N'U') is not null drop table {table_names.staging_table};"
    )


def _render_end_artifact_cleanup(table_names, has_primary_key: bool) -> str:
    cleanup = _render_start_artifact_cleanup(table_names)
    if not has_primary_key:
        return cleanup

    return (
        "if not exists (\n"
        "    select 1\n"
        f"    from {table_names.reject_table}\n"
        ")\n"
        "begin\n"
        f"{_indent_sql(cleanup, 4)}\n"
        "end;"
    )


def _render_installer_column_metadata_sql(
    table_names,
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

    return render_sql_template(
        "etl/column_metadata",
        current_table_literal=_sql_string_literal(table_names.current_table),
        history_table_literal=_sql_string_literal(table_names.history_table),
        source_column_filter=source_column_filter,
        update_select=update_select,
    )


def _render_static_full_refresh_load_body(*, table_names) -> str:
    return render_sql_template(
        "etl/full_refresh_body",
        current_table=table_names.current_table,
        staging_table=table_names.staging_table,
    )


def _render_static_primary_key_load_body(
    *,
    table_names,
    primary_key_columns: list[str],
    is_incremental: bool,
) -> str:
    staging_target_join = _pk_join_predicate("s", "t", primary_key_columns)
    current_upsert_join = _pk_join_predicate("c", "u", primary_key_columns)
    history_upsert_join = _pk_join_predicate("h", "u", primary_key_columns)
    staging_current_join = _pk_join_predicate("s", "c", primary_key_columns)
    staging_history_join = _pk_join_predicate("s", "h", primary_key_columns)
    staging_blank_case_predicate = _pk_blank_predicate(
        "s",
        primary_key_columns,
        line_indent="                ",
        closing_indent="            ",
    )
    staging_blank_where_predicate = _pk_blank_predicate(
        "s",
        primary_key_columns,
        line_indent="            ",
        closing_indent="        ",
    )
    duplicate_partition_columns = _pk_partition_columns("s", primary_key_columns)
    target_missing_predicate = f"t.{_quote_identifier_part(primary_key_columns[0])} is null"
    delete_missing_filter = (
        f"not exists (select 1 from {table_names.accepted_table} as s "
        f"where {staging_current_join})"
    )
    delete_history_unwind_filter = (
        f"not exists (select 1 from {table_names.accepted_table} as s "
        f"where {staging_history_join})"
    )

    return render_sql_template(
        "etl/primary_key_body",
        upsert_table=table_names.upsert_table,
        staging_table=table_names.staging_table,
        accepted_table=table_names.accepted_table,
        reject_table=table_names.reject_table,
        current_table=table_names.current_table,
        history_table=table_names.history_table,
        view_name=table_names.view_name,
        target_missing_predicate=target_missing_predicate,
        staging_target_join=staging_target_join,
        staging_blank_case_predicate=staging_blank_case_predicate,
        staging_blank_where_predicate=staging_blank_where_predicate,
        duplicate_partition_columns=duplicate_partition_columns,
        current_upsert_join=current_upsert_join,
        history_upsert_join=history_upsert_join,
        missing_reconciliation=(
            ""
            if is_incremental
            else _render_missing_reconciliation(
                table_names, delete_missing_filter, delete_history_unwind_filter
            )
        ),
    )


def _render_missing_reconciliation(
    table_names, delete_missing_filter: str, delete_history_unwind_filter: str
) -> str:
    return f"""begin try
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


def _pk_blank_predicate(
    alias: str,
    columns: list[str],
    *,
    line_indent: str,
    closing_indent: str,
) -> str:
    predicates = [
        f"nullif(trim(cast({alias}.{_quote_identifier_part(column)} as varchar(max))), '') is null"
        for column in columns
    ]
    if not predicates:
        return ""
    if len(predicates) == 1:
        return predicates[0]
    lines = [f"{line_indent}{predicates[0]}"]
    lines.extend(f"{line_indent}or {predicate}" for predicate in predicates[1:])
    return "(\n" + "\n".join(lines) + f"\n{closing_indent})"


def _pk_partition_columns(alias: str, columns: list[str]) -> str:
    return ", ".join(f"{alias}.{_quote_identifier_part(column)}" for column in columns)
