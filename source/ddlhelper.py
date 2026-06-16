"""Helpers for generating T-SQL DDL around query text."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import uuid

import yaml

from source.sqlwrangle import insert_select_into, insert_where_one_eq_zero


DEFAULT_TYPE_MAPPING_PATH = (
    Path(__file__).resolve().parents[1] / "config" / "warehouse_type_mapping.yml"
)


@dataclass(frozen=True)
class _TableNames:
    view_name: str
    current_table: str
    history_table: str
    current_pk_constraint: str


def wrap_create_or_alter_view(sql_text: str, view_name: str) -> str:
    """Wrap query text in a simple CREATE OR ALTER VIEW statement."""

    body = _normalise_view_body(sql_text)
    return f"CREATE OR ALTER VIEW {_quote_multipart_identifier(view_name)} AS\n{body}"


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
        "/* Weaver generated table-shape inference script. */\n"
        "SET NOCOUNT ON;\n\n"
        f"IF OBJECT_ID('tempdb..{temp_table_name}') IS NOT NULL DROP TABLE {temp_table_name};\n\n"
        f"{shape_sql}\n\n"
        f"{create_sql}\n"
        f"\nDROP TABLE {temp_table_name};\n"
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
        identity_type = mapping.get("identity_type", "bigint IDENTITY NOT NULL")
        current_column_definitions.append(f"{quoted_identity} {identity_type}")
        history_column_definitions.append(
            f"{quoted_identity} {_history_identity_type(identity_type)}"
        )
        view_columns.append(quoted_identity)

    for row, column_name in zip(visible_rows, names):
        warehouse_type = _translate_described_type(row, mapping)
        is_primary_key_column = column_name.lower() in primary_key_lookup
        nullability = "NOT NULL" if is_primary_key_column or not row.get("is_nullable") else "NULL"
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
        "[Row insert datetime] datetime2(7) NULL",
        "[Row update datetime] datetime2(7) NULL",
        "[Row delete datetime] datetime2(7) NOT NULL DEFAULT '9999-12-31 00:00:00'",
    ]


def _history_row_datetime_definitions() -> list[str]:
    return [
        "[Row insert datetime] datetime2(7) NULL",
        "[Row update datetime] datetime2(7) NULL",
        "[Row delete datetime] datetime2(7) NOT NULL",
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
    joined_current_columns = ",\n".join(
        f"    {definition}" for definition in current_column_definitions
    )
    joined_history_columns = ",\n".join(
        f"    {definition}" for definition in history_column_definitions
    )
    joined_view_columns = ",\n".join(f"    {column}" for column in view_columns)
    primary_key_sql = _render_primary_key_sql(
        table_names=table_names,
        primary_key_columns=primary_key_columns,
    )
    primary_key_section = f"\n\n{primary_key_sql}" if primary_key_sql else ""

    return (
        f"CREATE TABLE {table_names.current_table} (\n"
        f"{joined_current_columns}\n"
        ");\n\n"
        f"CREATE TABLE {table_names.history_table} (\n"
        f"{joined_history_columns}\n"
        ");"
        f"{primary_key_section}\n\n"
        f"CREATE OR ALTER VIEW {table_names.view_name} AS\n"
        "SELECT\n"
        f"{joined_view_columns}\n"
        f"FROM {table_names.current_table};"
    )


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
        f"ALTER TABLE {table_names.current_table} "
        f"ADD CONSTRAINT {table_names.current_pk_constraint} "
        f"PRIMARY KEY NONCLUSTERED ({joined_key_columns}) NOT ENFORCED;"
    )


def _render_primary_key_columns_cte(primary_key_columns: list[str]) -> str:
    if not primary_key_columns:
        return (
            "    SELECT\n"
            "        CONVERT(int, NULL) AS column_ordinal,\n"
            "        CONVERT(nvarchar(128), NULL) AS column_name\n"
            "    WHERE 1 = 0"
        )

    values = ",\n".join(
        f"        ({index}, {_sql_string_literal(column_name)})"
        for index, column_name in enumerate(primary_key_columns, start=1)
    )
    return (
        "    SELECT\n"
        "        column_ordinal,\n"
        "        column_name\n"
        "    FROM (VALUES\n"
        f"{values}\n"
        "    ) AS pk(column_ordinal, column_name)"
    )


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
    identity_type = mapping.get("identity_type", "bigint IDENTITY NOT NULL")
    history_identity_type = _history_identity_type(identity_type)
    identity_literal = (
        "NULL" if identity_column is None else _sql_string_literal(identity_column)
    )
    type_case = _render_type_mapping_case(mapping)
    temp_object_literal = _sql_string_literal(f"tempdb..{temp_table_name}")
    primary_key_columns_cte = _render_primary_key_columns_cte(primary_key_columns)

    return f"""DECLARE @weaver_identity_column varchar(128) = {identity_literal};
DECLARE @weaver_current_create_sql nvarchar(max);
DECLARE @weaver_history_create_sql nvarchar(max);
DECLARE @weaver_current_pk_sql nvarchar(max);
DECLARE @weaver_view_sql nvarchar(max);

IF NOT EXISTS (
    SELECT 1
    FROM tempdb.sys.columns AS c
    WHERE c.[object_id] = OBJECT_ID({temp_object_literal})
)
BEGIN
    THROW 51001, 'Weaver found no temp table columns to create.', 1;
END;

;WITH primary_key_columns AS (
{primary_key_columns_cte}
),
raw_described AS (
    SELECT
        c.column_id AS column_ordinal,
        COALESCE(NULLIF(c.name, ''), CONCAT('Column', c.column_id)) AS column_name,
        t.name AS system_type_name,
        c.max_length,
        c.precision,
        c.scale,
        c.is_nullable
    FROM tempdb.sys.columns AS c
    INNER JOIN tempdb.sys.types AS t
        ON t.user_type_id = c.user_type_id
    WHERE c.[object_id] = OBJECT_ID({temp_object_literal})
),
described AS (
    SELECT
        column_ordinal,
        CASE
            WHEN COUNT(*) OVER (PARTITION BY column_name) = 1 THEN column_name
            ELSE CONCAT(
                column_name,
                '_',
                ROW_NUMBER() OVER (PARTITION BY column_name ORDER BY column_ordinal)
            )
        END AS column_name,
        system_type_name,
        max_length,
        precision,
        scale,
        is_nullable
    FROM raw_described
),
mapped AS (
    SELECT
        d.column_ordinal,
        QUOTENAME(d.column_name) AS quoted_column_name,
        {type_case} AS warehouse_type,
        CASE
            WHEN pk.column_name IS NOT NULL OR d.is_nullable = 0 THEN N' NOT NULL'
            ELSE N' NULL'
        END AS nullability
    FROM described AS d
    LEFT JOIN primary_key_columns AS pk
        ON LOWER(pk.column_name) = LOWER(d.column_name)
    CROSS APPLY (
        SELECT
            LOWER(d.system_type_name) AS base_type
    ) AS bt
),
source_column_definitions AS (
    SELECT
        column_ordinal + CASE WHEN @weaver_identity_column IS NULL THEN 0 ELSE 1 END AS column_ordinal,
        quoted_column_name,
        quoted_column_name + N' ' + warehouse_type + nullability AS current_column_definition,
        quoted_column_name + N' ' + warehouse_type + nullability AS history_column_definition,
        quoted_column_name AS view_column
    FROM mapped
),
all_columns AS (
    SELECT
        1 AS column_ordinal,
        QUOTENAME(@weaver_identity_column) AS quoted_column_name,
        QUOTENAME(@weaver_identity_column) + N' {identity_type}' AS current_column_definition,
        QUOTENAME(@weaver_identity_column) + N' {history_identity_type}' AS history_column_definition,
        QUOTENAME(@weaver_identity_column) AS view_column
    WHERE @weaver_identity_column IS NOT NULL

    UNION ALL

    SELECT
        column_ordinal,
        quoted_column_name,
        current_column_definition,
        history_column_definition,
        view_column
    FROM source_column_definitions

    UNION ALL

    SELECT
        1000001,
        N'[Row insert datetime]',
        N'[Row insert datetime] datetime2(7) NULL',
        N'[Row insert datetime] datetime2(7) NULL',
        N'[Row insert datetime]'

    UNION ALL

    SELECT
        1000002,
        N'[Row update datetime]',
        N'[Row update datetime] datetime2(7) NULL',
        N'[Row update datetime] datetime2(7) NULL',
        N'[Row update datetime]'

    UNION ALL

    SELECT
        1000003,
        N'[Row delete datetime]',
        N'[Row delete datetime] datetime2(7) NOT NULL DEFAULT ''9999-12-31 00:00:00''',
        N'[Row delete datetime] datetime2(7) NOT NULL',
        NULL
)
SELECT
    @weaver_current_create_sql = (
        SELECT
            N'CREATE TABLE {table_names.current_table} (' + CHAR(10)
            + STRING_AGG(N'    ' + current_column_definition, N',' + CHAR(10))
                WITHIN GROUP (ORDER BY column_ordinal)
            + CHAR(10) + N');'
        FROM all_columns
    ),
    @weaver_history_create_sql = (
        SELECT
            N'CREATE TABLE {table_names.history_table} (' + CHAR(10)
            + STRING_AGG(N'    ' + history_column_definition, N',' + CHAR(10))
                WITHIN GROUP (ORDER BY column_ordinal)
            + CHAR(10) + N');'
        FROM all_columns
    ),
    @weaver_view_sql = (
        SELECT
            N'CREATE OR ALTER VIEW {table_names.view_name} AS' + CHAR(10)
            + N'SELECT' + CHAR(10)
            + STRING_AGG(N'    ' + view_column, N',' + CHAR(10))
                WITHIN GROUP (ORDER BY column_ordinal)
            + CHAR(10) + N'FROM {table_names.current_table};'
        FROM all_columns
        WHERE view_column IS NOT NULL
    ),
    @weaver_current_pk_sql = (
        SELECT
            N'ALTER TABLE {table_names.current_table} ADD CONSTRAINT {table_names.current_pk_constraint} '
            + N'PRIMARY KEY NONCLUSTERED ('
            + STRING_AGG(QUOTENAME(column_name), N', ') WITHIN GROUP (ORDER BY column_ordinal)
            + N') NOT ENFORCED;'
        FROM primary_key_columns
    );

PRINT @weaver_current_create_sql;
EXEC sys.sp_executesql @weaver_current_create_sql;

PRINT @weaver_history_create_sql;
EXEC sys.sp_executesql @weaver_history_create_sql;

IF @weaver_current_pk_sql IS NOT NULL
BEGIN
    PRINT @weaver_current_pk_sql;
    EXEC sys.sp_executesql @weaver_current_pk_sql;
END;

PRINT @weaver_view_sql;
EXEC sys.sp_executesql @weaver_view_sql;"""


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
    lines = ["CASE bt.base_type"]
    for source_type in sorted(mappings):
        expression = _render_target_type_expression(source_type, mappings[source_type])
        lines.append(f"            WHEN '{source_type.lower()}' THEN {expression}")
    lines.append(f"            ELSE N'{_escape_sql_literal(fallback)}'")
    lines.append("        END")
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
            f"CONVERT(nvarchar(20), "
            f"COALESCE(NULLIF(CONVERT(int, d.{column_name}), 0), {default_value}))"
        )
    return f"N'{value}'"


def _scale_expression(value: str | int) -> str:
    if value == "min_source_6":
        return (
            "CONVERT(nvarchar(20), "
            "CASE "
            "WHEN d.scale IS NULL THEN 6 "
            "WHEN CONVERT(int, d.scale) > 6 THEN 6 "
            "WHEN CONVERT(int, d.scale) < 0 THEN 0 "
            "ELSE CONVERT(int, d.scale) "
            "END)"
        )
    return f"N'{value}'"


def _length_expression(source_type: str, value: str | int) -> str:
    if value == "max":
        return "N'max'"
    if value == "source":
        divisor = "2" if source_type.lower() in {"nchar", "nvarchar"} else "1"
        source_length = f"CONVERT(int, d.max_length) / {divisor}"
        return (
            "CASE "
            "WHEN d.max_length = -1 THEN N'max' "
            "WHEN d.max_length IS NULL OR d.max_length = 0 THEN N'1' "
            f"ELSE CONVERT(nvarchar(20), CASE WHEN {source_length} < 1 THEN 1 ELSE {source_length} END) "
            "END"
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
