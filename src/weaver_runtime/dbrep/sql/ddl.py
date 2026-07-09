"""Helpers for generating T-SQL DDL around query text."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
import re
import uuid

import yaml

from ._ses_compat import (
    SesMetadata,
    SesRepository,
    SesSqlDocument,
    SesSyntaxException,
)
from .wrangle import (
    SqlDependency,
    insert_select_into,
    insert_where_one_eq_zero,
    render_sql_template,
)


DEFAULT_TYPE_MAPPING_PATH = (
    Path(__file__).resolve().parent / "warehouse_type_mapping.yml"
)


@dataclass(frozen=True)
class _TableNames:
    view_name: str
    current_table: str
    history_table: str
    staging_table: str
    upsert_table: str
    reject_table: str
    load_procedure: str
    current_pk_constraint: str


def wrap_create_or_alter_view(sql_text: str, view_name: str) -> str:
    """Wrap query text in a simple CREATE OR ALTER VIEW statement."""

    body = _normalise_view_body(sql_text)
    return f"create or alter view {_quote_multipart_identifier(view_name)} as\n{body}"


def generate_infer_create_table_sql(
    sql_text: str | SesSqlDocument,
    target_table_name: str | SesMetadata | None = None,
    *,
    identity_column: str | None = None,
    primary_key_columns: list[str] | None = None,
    temp_table_name: str | None = None,
    type_mapping_path: str | Path | None = None,
) -> str:
    """Generate a self-contained SQL script that infers and creates a table."""

    sql_text, target_table_name, metadata = _coerce_table_generator_inputs(
        sql_text,
        target_table_name,
        identity_column=identity_column,
        primary_key_columns=primary_key_columns,
    )
    identity_column = identity_column if metadata is None else metadata.identity
    primary_key_columns = (
        primary_key_columns if metadata is None else list(metadata.primary_key)
    )

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
        metadata=metadata,
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


def generate_ses_repository_ddl_sql(
    repository: SesRepository | str | Path,
    *,
    temp_table_name_prefix: str = "#weaver_shape",
) -> list[list[str]]:
    """Generate executable DDL batches for an SES repository.

    The outer list must run serially. SQL strings within one inner list have no
    SES dependency on one another and may run in parallel.
    """

    repository = (
        repository if isinstance(repository, SesRepository) else SesRepository(repository)
    )
    documents = repository.iter_documents()
    layers = _topological_ses_document_layers(documents)
    sql_layers = [
        [
            _render_ses_object_ddl(
                document,
                temp_table_name_prefix=temp_table_name_prefix,
            )
            for document in layer
        ]
        for layer in layers
    ]

    procedure_layer = [
        _render_ses_load_procedure_ddl(document)
        for document in sorted(
            documents,
            key=lambda item: item.metadata.qualified_name,
        )
        if document.metadata.is_table
    ]
    if procedure_layer:
        sql_layers.append(procedure_layer)

    return sql_layers


def generate_ses_repository_ddl(
    repository: SesRepository | str | Path,
    *,
    temp_table_name_prefix: str = "#weaver_shape",
) -> list[list[str]]:
    """Alias for ``generate_ses_repository_ddl_sql``."""

    return generate_ses_repository_ddl_sql(
        repository,
        temp_table_name_prefix=temp_table_name_prefix,
    )


def _coerce_table_generator_inputs(
    sql_text: str | SesSqlDocument,
    target_table_name: str | SesMetadata | None,
    *,
    identity_column: str | None,
    primary_key_columns: list[str] | None,
) -> tuple[str, str, SesMetadata | None]:
    if isinstance(sql_text, SesSqlDocument):
        metadata = sql_text.metadata
        _require_table_metadata(metadata)
        if target_table_name is not None:
            raise ValueError(
                "target_table_name must not be supplied when using SesSqlDocument"
            )
        return sql_text.sql_text, metadata.qualified_name, metadata

    if isinstance(target_table_name, SesMetadata):
        metadata = target_table_name
        _require_table_metadata(metadata)
        return sql_text, metadata.qualified_name, metadata

    if target_table_name is None:
        raise ValueError(
            "target_table_name is required unless sql_text is a SesSqlDocument"
        )
    return sql_text, target_table_name, None


def _require_table_metadata(metadata: SesMetadata) -> None:
    if not metadata.is_table:
        raise ValueError("SES metadata must use Table ID to generate table DDL")


def _topological_ses_document_layers(
    documents: tuple[SesSqlDocument, ...],
) -> list[list[SesSqlDocument]]:
    documents_by_name = _ses_documents_by_dependency_name(documents)
    remaining = set(documents_by_name)
    built: set[SqlDependency] = set()
    layers: list[list[SesSqlDocument]] = []

    while remaining:
        ready_names = sorted(
            dependency_name
            for dependency_name in remaining
            if documents_by_name[dependency_name].ses_dependencies <= built
        )
        if not ready_names:
            cycle = ", ".join(
                _format_unquoted_dependency_name(dependency_name)
                for dependency_name in sorted(remaining)
            )
            raise SesSyntaxException(f"SES dependency cycle detected: {cycle}")

        layers.append(
            [documents_by_name[dependency_name] for dependency_name in ready_names]
        )
        built.update(ready_names)
        remaining.difference_update(ready_names)

    return layers


def _ses_documents_by_dependency_name(
    documents: tuple[SesSqlDocument, ...],
) -> dict[SqlDependency, SesSqlDocument]:
    return {
        (document.metadata.schema, document.metadata.name): document
        for document in documents
    }


def _format_unquoted_dependency_name(dependency_name: SqlDependency) -> str:
    return ".".join(dependency_name)


def _render_ses_object_ddl(
    document: SesSqlDocument,
    *,
    temp_table_name_prefix: str,
) -> str:
    if document.metadata.is_table:
        temp_table_name = _ses_temp_table_name(
            temp_table_name_prefix,
            document.metadata.qualified_name,
        )
        return generate_infer_create_table_sql(
            document,
            temp_table_name=temp_table_name,
        )

    return wrap_create_or_alter_view(
        document.sql_text,
        document.metadata.qualified_name,
    )


def _render_ses_load_procedure_ddl(document: SesSqlDocument) -> str:
    from .etl import generate_load_stored_procedure_sql

    return generate_load_stored_procedure_sql(
        document.sql_text,
        document.metadata.qualified_name,
        primary_key_columns=list(document.metadata.primary_key),
    )


def _ses_temp_table_name(prefix: str, qualified_name: str) -> str:
    normalised_prefix = prefix if prefix.startswith("#") else f"#{prefix}"
    safe_name = re.sub(r"[^A-Za-z0-9_]", "_", qualified_name)
    candidate = f"{normalised_prefix}_{safe_name}"
    if len(candidate) > 111:
        digest = hashlib.sha1(qualified_name.encode("utf-8")).hexdigest()[:12]
        candidate = f"{candidate[:98]}_{digest}"
    return _normalise_temp_table_name(candidate)


def build_create_table_sql_from_describe_rows(
    describe_rows: list[dict],
    target_table_name: str | SesMetadata,
    *,
    identity_column: str | None = None,
    primary_key_columns: list[str] | None = None,
    type_mapping_path: str | Path | None = None,
) -> str:
    """Build Fabric table/view DDL from described result-set rows."""

    metadata = target_table_name if isinstance(target_table_name, SesMetadata) else None
    if metadata is not None:
        _require_table_metadata(metadata)
        target_table_name = metadata.qualified_name
        identity_column = metadata.identity
        primary_key_columns = list(metadata.primary_key)

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
    if metadata is not None:
        _validate_metadata_columns_exist(metadata, names)

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
        reject_table=f"{quoted_schema}.{_quote_identifier_part(f'{unquoted_table_name}_Reject')}",
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

    return render_sql_template(
        "ddl/backing_table_and_view",
        current_table=table_names.current_table,
        current_columns=joined_current_columns,
        history_table=table_names.history_table,
        history_columns=joined_history_columns,
        post_create_section=post_create_section,
        view_name=table_names.view_name,
        view_columns=joined_view_columns,
    ).rstrip()


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


def _metadata_referenced_columns(metadata: SesMetadata) -> list[tuple[str, str]]:
    columns: list[tuple[str, str]] = []
    columns.extend(("Primary key", column) for column in metadata.primary_key)
    for unique_key in metadata.unique_keys:
        columns.extend(("Unique key", column) for column in unique_key)
    for foreign_key in metadata.foreign_keys:
        columns.extend(("Foreign key", column) for column in foreign_key.child_columns)
    columns.extend(("Column notes", column) for column in metadata.column_notes)
    return columns


def _validate_metadata_columns_exist(
    metadata: SesMetadata,
    column_names: list[str],
) -> None:
    available_columns = {column_name.lower() for column_name in column_names}
    if metadata.identity:
        available_columns.add(metadata.identity.lower())

    for metadata_kind, column_name in _metadata_referenced_columns(metadata):
        if column_name.lower() not in available_columns:
            raise ValueError(f"{metadata_kind} {column_name} does not exist")


def _indent_sql(sql_text: str, spaces: int) -> str:
    prefix = " " * spaces
    return "\n".join(f"{prefix}{line}" if line else "" for line in sql_text.splitlines())


def _render_metadata_column_validation_sql(
    *,
    temp_object_literal: str,
    metadata: SesMetadata | None,
) -> str:
    if metadata is None:
        return ""

    referenced_columns = _metadata_referenced_columns(metadata)
    if not referenced_columns:
        return ""

    metadata_columns_cte = _render_metadata_columns_cte(referenced_columns)
    return render_sql_template(
        "ddl/metadata_column_validation",
        temp_object_literal=temp_object_literal,
        metadata_columns_cte=metadata_columns_cte,
    ).rstrip()


def _render_metadata_columns_cte(referenced_columns: list[tuple[str, str]]) -> str:
    lines = []
    for index, (metadata_kind, column_name) in enumerate(referenced_columns):
        prefix = "    select" if index == 0 else "    union all\n\n    select"
        lines.append(
            f"{prefix}\n"
            f"        {_sql_string_literal(metadata_kind)} as metadata_kind\n"
            f"      , {_sql_string_literal(column_name)} as column_name"
        )
    return "\n".join(lines)


def _render_infer_create_sql(
    *,
    temp_table_name: str,
    target_table_name: str,
    identity_column: str | None,
    primary_key_columns: list[str] | None,
    metadata: SesMetadata | None,
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
    metadata_validation_sql = _render_metadata_column_validation_sql(
        temp_object_literal=temp_object_literal,
        metadata=metadata,
    )

    return render_sql_template(
        "ddl/infer_create_table",
        identity_literal=identity_literal,
        temp_object_literal=temp_object_literal,
        metadata_validation_sql=metadata_validation_sql,
        primary_key_columns_cte=primary_key_columns_cte,
        type_case=type_case,
        identity_type=identity_type,
        history_identity_type=history_identity_type,
        current_table=table_names.current_table,
        current_table_literal=_sql_string_literal(table_names.current_table),
        history_table=table_names.history_table,
        history_table_literal=_sql_string_literal(table_names.history_table),
        view_name=table_names.view_name,
        current_pk_constraint=table_names.current_pk_constraint,
    ).rstrip()


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
