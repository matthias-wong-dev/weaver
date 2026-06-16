"""Utilities for transforming T-SQL text."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import uuid

import sqlparse
from sqlparse import tokens as T
import yaml


DEFAULT_TYPE_MAPPING_PATH = (
    Path(__file__).resolve().parents[1] / "config" / "warehouse_type_mapping.yml"
)


_BOUNDARY_KEYWORDS = {
    "GO",
    "GROUP",
    "HAVING",
    "ORDER",
    "UNION",
    "EXCEPT",
    "INTERSECT",
    "OPTION",
    "FOR",
}

_STATEMENT_START_KEYWORDS = {
    "ALTER",
    "CREATE",
    "DECLARE",
    "DELETE",
    "DROP",
    "EXEC",
    "EXECUTE",
    "IF",
    "INSERT",
    "MERGE",
    "PRINT",
    "RAISERROR",
    "RETURN",
    "SELECT",
    "SET",
    "THROW",
    "TRUNCATE",
    "UPDATE",
    "USE",
    "WAITFOR",
    "WHILE",
}


@dataclass(frozen=True)
class _FlatToken:
    value: str
    normalized: str
    ttype: object
    start: int
    end: int
    depth: int


@dataclass(frozen=True)
class _Replacement:
    start: int
    end: int
    text: str


@dataclass(frozen=True)
class _QuerySpan:
    start: int
    end: int
    select_index: int


def insert_where_one_eq_zero(sql_text: str) -> str:
    """Insert ``WHERE 1=0`` into every SELECT in a T-SQL string.

    If a SELECT already has a WHERE clause, its existing condition is wrapped in
    parentheses and combined with ``AND 1=0``.
    """

    replacements = _collect_replacements(sql_text)
    if not replacements:
        return sql_text

    result = sql_text
    for replacement in sorted(replacements, key=lambda item: item.start, reverse=True):
        result = (
            result[: replacement.start] + replacement.text + result[replacement.end :]
        )
    return result


def insert_ctas(sql_text: str, table_name: str) -> str:
    """Prefix the last standalone SELECT query with Fabric CTAS syntax."""

    query_span = _find_last_standalone_query(sql_text)
    if query_span is None:
        return sql_text

    return (
        f"{sql_text[:query_span.start]}CREATE TABLE {table_name} AS\n"
        f"{sql_text[query_span.start:]}"
    )


def insert_select_into(sql_text: str, table_name: str) -> str:
    """Insert ``INTO <table_name>`` into the last standalone SELECT query."""

    query_span = _find_last_standalone_query(sql_text)
    if query_span is None:
        return sql_text

    tokens = _flatten_with_offsets(sql_text)
    insert_at = _find_select_into_insert_position(tokens, query_span)
    insert_text = _select_into_text(sql_text, insert_at, table_name)
    return f"{sql_text[:insert_at]}{insert_text}{sql_text[insert_at:]}"


def generate_infer_create_table_sql(
    sql_text: str,
    target_table_name: str,
    *,
    identity_column: str | None = None,
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


def _collect_replacements(sql_text: str) -> list[_Replacement]:
    tokens = _flatten_with_offsets(sql_text)
    replacements: list[_Replacement] = []
    covered_ranges: list[tuple[int, int]] = []

    for index, token in enumerate(tokens):
        if not _is_select(token):
            continue

        if _is_covered(token.start, covered_ranges):
            continue

        replacement = _replacement_for_select(sql_text, tokens, index)
        if replacement is None:
            continue

        replacements = [
            item
            for item in replacements
            if not (replacement.start <= item.start and item.end <= replacement.end)
        ]
        if replacement.start != replacement.end:
            covered_ranges.append((replacement.start, replacement.end))
        replacements.append(replacement)

    return replacements


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


def _render_infer_create_sql(
    *,
    temp_table_name: str,
    target_table_name: str,
    identity_column: str | None,
    mapping: dict,
) -> str:
    quoted_target = _quote_multipart_identifier(target_table_name)
    identity_type = mapping.get("identity_type", "bigint IDENTITY NOT NULL")
    identity_literal = (
        "NULL" if identity_column is None else _sql_string_literal(identity_column)
    )
    type_case = _render_type_mapping_case(mapping)
    temp_object_literal = _sql_string_literal(f"tempdb..{temp_table_name}")

    return f"""DECLARE @weaver_identity_column varchar(128) = {identity_literal};
DECLARE @weaver_create_sql nvarchar(max);

;WITH raw_described AS (
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
        CASE WHEN d.is_nullable = 1 THEN N' NULL' ELSE N' NOT NULL' END AS nullability
    FROM described AS d
    CROSS APPLY (
        SELECT
            LOWER(d.system_type_name) AS base_type
    ) AS bt
),
column_definitions AS (
    SELECT
        1 AS column_ordinal,
        QUOTENAME(@weaver_identity_column) + N' {identity_type}' AS column_definition
    WHERE @weaver_identity_column IS NOT NULL

    UNION ALL

    SELECT
        column_ordinal + CASE WHEN @weaver_identity_column IS NULL THEN 0 ELSE 1 END,
        quoted_column_name + N' ' + warehouse_type + nullability
    FROM mapped
)
SELECT
    @weaver_create_sql =
        N'CREATE TABLE {quoted_target} (' + CHAR(10)
        + STRING_AGG(N'    ' + column_definition, N',' + CHAR(10))
            WITHIN GROUP (ORDER BY column_ordinal)
        + CHAR(10) + N');'
FROM column_definitions;

IF @weaver_create_sql IS NULL
BEGIN
    THROW 51001, 'Weaver found no temp table columns to create.', 1;
END;

PRINT @weaver_create_sql;
EXEC sys.sp_executesql @weaver_create_sql;"""


def build_create_table_sql_from_describe_rows(
    describe_rows: list[dict],
    target_table_name: str,
    *,
    identity_column: str | None = None,
    type_mapping_path: str | Path | None = None,
) -> str:
    """Build Fabric CREATE TABLE SQL from sp_describe_first_result_set rows."""

    mapping = _load_type_mapping(type_mapping_path)
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

    column_definitions: list[str] = []
    if identity_column:
        column_definitions.append(
            f"{_quote_identifier_part(identity_column)} {mapping.get('identity_type', 'bigint IDENTITY NOT NULL')}"
        )

    for row, column_name in zip(visible_rows, names):
        warehouse_type = _translate_described_type(row, mapping)
        nullability = "NULL" if row.get("is_nullable") else "NOT NULL"
        column_definitions.append(
            f"{_quote_identifier_part(column_name)} {warehouse_type} {nullability}"
        )

    joined_columns = ",\n".join(f"    {definition}" for definition in column_definitions)
    return f"CREATE TABLE {_quote_multipart_identifier(target_table_name)} (\n{joined_columns}\n);"


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


def _find_last_standalone_query(sql_text: str) -> _QuerySpan | None:
    tokens = _flatten_with_offsets(sql_text)
    spans: list[_QuerySpan] = []

    for index, token in enumerate(tokens):
        if token.depth != 0:
            continue

        if _is_select(token) and _is_standalone_select_start(tokens, index):
            spans.append(_QuerySpan(token.start, _find_query_end(tokens, index), index))
            continue

        if _keyword_head(token) == "WITH" and _is_statement_boundary_before(
            tokens, index
        ):
            select_index = _find_cte_body_select(tokens, index)
            if select_index is not None:
                spans.append(
                    _QuerySpan(
                        token.start, _find_query_end(tokens, select_index), select_index
                    )
                )

    if not spans:
        return None

    return max(spans, key=lambda item: item.start)


def _find_select_into_insert_position(
    tokens: list[_FlatToken], query_span: _QuerySpan
) -> int:
    select_token = tokens[query_span.select_index]

    for index in range(query_span.select_index + 1, len(tokens)):
        token = tokens[index]
        if token.start >= query_span.end:
            break
        if token.depth != select_token.depth:
            continue
        if _keyword_head(token) == "FROM":
            return _end_before_trivia(tokens, query_span.select_index + 1, index)
        if _is_select_into_boundary(token):
            return _end_before_trivia(tokens, query_span.select_index + 1, index)

    end_index = _token_index_at_or_after(tokens, query_span.end)
    return _end_before_trivia(tokens, query_span.select_index + 1, end_index)


def _select_into_text(sql_text: str, insert_at: int, table_name: str) -> str:
    if insert_at > 0 and sql_text[insert_at - 1] == "\n":
        return f"INTO {table_name}\n"
    next_non_space = insert_at
    while next_non_space < len(sql_text) and sql_text[next_non_space] in " \t\r\n":
        if sql_text[next_non_space] == "\n":
            return f"\nINTO {table_name}"
        next_non_space += 1
    return f" INTO {table_name}"


def _is_select_into_boundary(token: _FlatToken) -> bool:
    keyword = _keyword_head(token)
    return token.value == ";" or keyword in {
        "WHERE",
        "GROUP",
        "HAVING",
        "ORDER",
        "UNION",
        "EXCEPT",
        "INTERSECT",
        "OPTION",
        "FOR",
        "GO",
    }


def _token_index_at_or_after(tokens: list[_FlatToken], position: int) -> int:
    for index, token in enumerate(tokens):
        if token.start >= position:
            return index
    return len(tokens)


def _find_cte_body_select(tokens: list[_FlatToken], with_index: int) -> int | None:
    for index in range(with_index + 1, len(tokens)):
        token = tokens[index]
        if token.depth != 0:
            continue
        if _is_select(token):
            return index
        if token.value == ";" or _keyword_head(token) == "GO":
            return None
        if _is_statement_starter(token) and _keyword_head(token) not in {"WITH", "SELECT"}:
            return None
    return None


def _find_query_end(tokens: list[_FlatToken], select_index: int) -> int:
    select_token = tokens[select_index]

    for index in range(select_index + 1, len(tokens)):
        token = tokens[index]
        if token.depth < select_token.depth:
            return token.start
        if token.depth != select_token.depth:
            continue
        if token.value == ";" or _keyword_head(token) == "GO":
            return token.end if token.value == ";" else _end_before_trivia(tokens, select_index, index)
        if _is_statement_starter(token) and not _is_set_operator_select(tokens, index):
            return _end_before_trivia(tokens, select_index, index)

    return _end_before_trivia(tokens, select_index, len(tokens))


def _is_standalone_select_start(tokens: list[_FlatToken], index: int) -> bool:
    if _is_set_operator_select(tokens, index):
        return False
    if _has_top_level_with_since_boundary(tokens, index):
        return False
    if _is_statement_boundary_before(tokens, index):
        return True
    if not _starts_new_line(tokens, index):
        return False

    starter = _last_statement_starter_since_boundary(tokens, index)
    if starter is None:
        return True
    if _keyword_head(tokens[starter]) == "INSERT":
        return _has_top_level_select_between(tokens, starter + 1, index)
    return True


def _is_set_operator_select(tokens: list[_FlatToken], index: int) -> bool:
    previous = _previous_significant_index(tokens, index)
    if previous is None:
        return False

    previous_keyword = _keyword_head(tokens[previous])
    if previous_keyword in {"UNION", "EXCEPT", "INTERSECT"}:
        return True
    if previous_keyword == "ALL":
        before_all = _previous_significant_index(tokens, previous)
        return before_all is not None and _keyword_head(tokens[before_all]) in {
            "UNION",
            "EXCEPT",
            "INTERSECT",
        }
    return False


def _is_statement_boundary_before(tokens: list[_FlatToken], index: int) -> bool:
    previous = _previous_significant_index(tokens, index)
    if previous is None:
        return True

    previous_token = tokens[previous]
    return previous_token.value == ";" or _keyword_head(previous_token) == "GO"


def _starts_new_line(tokens: list[_FlatToken], index: int) -> bool:
    for previous in range(index - 1, -1, -1):
        token = tokens[previous]
        if token.depth != tokens[index].depth:
            continue
        if "\n" in token.value:
            return True
        if not _is_trivia(token):
            return False
    return True


def _last_statement_starter_since_boundary(
    tokens: list[_FlatToken], index: int
) -> int | None:
    for previous in range(index - 1, -1, -1):
        token = tokens[previous]
        if token.depth != tokens[index].depth or _is_trivia(token):
            continue
        if token.value == ";" or _keyword_head(token) == "GO":
            return None
        if _is_statement_starter(token):
            return previous
    return None


def _has_top_level_select_between(
    tokens: list[_FlatToken], start: int, end: int
) -> bool:
    return any(token.depth == 0 and _is_select(token) for token in tokens[start:end])


def _has_top_level_with_since_boundary(tokens: list[_FlatToken], index: int) -> bool:
    for previous in range(index - 1, -1, -1):
        token = tokens[previous]
        if token.depth != tokens[index].depth or _is_trivia(token):
            continue
        if token.value == ";" or _keyword_head(token) == "GO":
            return False
        if _keyword_head(token) == "WITH":
            return True
    return False


def _replacement_for_select(
    sql_text: str, tokens: list[_FlatToken], select_index: int
) -> _Replacement | None:
    select_token = tokens[select_index]
    scope_end_index = _find_scope_end(tokens, select_index)
    where_index = _find_where(tokens, select_index + 1, scope_end_index, select_token.depth)

    if where_index is None:
        insert_at = _find_insert_position(
            tokens, select_index + 1, scope_end_index, select_token.depth
        )
        return _Replacement(insert_at, insert_at, " WHERE 1=0")

    condition_start = tokens[where_index].end
    condition_end = _find_condition_end(
        tokens, where_index + 1, scope_end_index, select_token.depth
    )
    condition = sql_text[condition_start:condition_end].strip()
    transformed_condition = insert_where_one_eq_zero(condition) if condition else condition
    return _Replacement(
        condition_start,
        condition_end,
        f" ({transformed_condition}) AND 1=0",
    )


def _flatten_with_offsets(sql_text: str) -> list[_FlatToken]:
    flat: list[_FlatToken] = []
    offset = 0
    depth = 0

    for statement in sqlparse.parse(sql_text):
        for token in statement.flatten():
            value = token.value
            token_depth = depth
            if value == ")":
                depth = max(0, depth - 1)
                token_depth = depth

            flat.append(
                _FlatToken(
                    value=value,
                    normalized=token.normalized.upper(),
                    ttype=token.ttype,
                    start=offset,
                    end=offset + len(value),
                    depth=token_depth,
                )
            )
            offset += len(value)

            if value == "(":
                depth += 1

    return flat


def _find_scope_end(tokens: list[_FlatToken], select_index: int) -> int:
    select_token = tokens[select_index]

    for index in range(select_index + 1, len(tokens)):
        token = tokens[index]
        if token.depth < select_token.depth:
            return index
        if token.depth == select_token.depth and _is_scope_terminator(token):
            return index

    return len(tokens)


def _find_where(
    tokens: list[_FlatToken], start: int, end: int, depth: int
) -> int | None:
    for index in range(start, end):
        token = tokens[index]
        if token.depth == depth and token.normalized == "WHERE":
            return index
    return None


def _find_insert_position(
    tokens: list[_FlatToken], start: int, end: int, depth: int
) -> int:
    for index in range(start, end):
        token = tokens[index]
        if token.depth == depth and _is_boundary(token):
            return _end_before_trivia(tokens, start, index)

    if end < len(tokens):
        return _end_before_trivia(tokens, start, end)
    if tokens:
        return _end_before_trivia(tokens, start, len(tokens))
    return 0


def _find_condition_end(
    tokens: list[_FlatToken], start: int, end: int, depth: int
) -> int:
    for index in range(start, end):
        token = tokens[index]
        if token.depth == depth and _is_boundary(token):
            return _end_before_trivia(tokens, start, index)

    if end < len(tokens):
        return _end_before_trivia(tokens, start, end)
    if tokens:
        return _end_before_trivia(tokens, start, len(tokens))
    return 0


def _end_before_trivia(tokens: list[_FlatToken], start: int, end: int) -> int:
    index = end - 1
    while index >= start and tokens[index].ttype in T.Whitespace:
        index -= 1
    if index >= start:
        return tokens[index].end
    return tokens[start].start if start < len(tokens) else 0


def _is_select(token: _FlatToken) -> bool:
    return token.ttype is T.DML and token.normalized == "SELECT"


def _is_boundary(token: _FlatToken) -> bool:
    return token.value == ";" or _keyword_head(token) in _BOUNDARY_KEYWORDS


def _is_scope_terminator(token: _FlatToken) -> bool:
    if token.value == ";":
        return True

    keyword = _keyword_head(token)
    return keyword in {"GO", "UNION", "EXCEPT", "INTERSECT"} or (
        _is_statement_starter(token) and keyword != "SELECT"
    ) or _is_select(token)


def _is_statement_starter(token: _FlatToken) -> bool:
    return _keyword_head(token) in _STATEMENT_START_KEYWORDS


def _keyword_head(token: _FlatToken) -> str:
    parts = token.normalized.split(maxsplit=1)
    return parts[0] if parts else ""


def _previous_significant_index(
    tokens: list[_FlatToken], index: int, depth: int = 0
) -> int | None:
    for previous in range(index - 1, -1, -1):
        token = tokens[previous]
        if token.depth != depth or _is_trivia(token):
            continue
        return previous
    return None


def _is_trivia(token: _FlatToken) -> bool:
    return token.ttype in T.Whitespace or token.ttype in T.Comment


def _is_covered(position: int, ranges: list[tuple[int, int]]) -> bool:
    return any(start <= position < end for start, end in ranges)
