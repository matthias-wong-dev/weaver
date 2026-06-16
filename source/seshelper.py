"""Helpers for reading SES SQL files with YAML metadata comment blocks."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any

import yaml

from source.sqlwrangle import (
    SqlDependency,
    find_sql_dependencies,
    format_sql_dependency,
)


class SesSyntaxException(ValueError):
    """Raised when an SES SQL file is syntactically invalid."""


class SesValidationError(SesSyntaxException):
    """Raised when an SES SQL file metadata block is missing or invalid."""


@dataclass(frozen=True)
class SesObjectId:
    """Schema/object identifier declared by Table ID or View ID."""

    schema: str
    name: str

    @property
    def qualified_name(self) -> str:
        return f"{self.schema}.{self.name}"


@dataclass(frozen=True)
class SesForeignKey:
    """Foreign-key metadata declared as child columns to a parent column set."""

    child_columns: tuple[str, ...]
    parent: SesObjectId
    parent_columns: tuple[str, ...]


@dataclass(frozen=True)
class SesMetadata:
    """Parsed SES metadata from the leading SQL comment block."""

    object_kind: str
    object_id: SesObjectId
    description: str
    revision_notes: tuple[str, ...]
    primary_key: tuple[str, ...]
    identity: str | None
    unique_keys: tuple[tuple[str, ...], ...]
    foreign_keys: tuple[SesForeignKey, ...]
    column_notes: dict[str, str]
    notes: str | None = None
    raw: dict[str, Any] | None = None

    @property
    def is_table(self) -> bool:
        return self.object_kind == "table"

    @property
    def is_view(self) -> bool:
        return self.object_kind == "view"

    @property
    def schema(self) -> str:
        return self.object_id.schema

    @property
    def name(self) -> str:
        return self.object_id.name

    @property
    def qualified_name(self) -> str:
        return self.object_id.qualified_name


@dataclass(frozen=True)
class SesSqlDocument:
    """A SQL file split into SES metadata and executable SQL text."""

    metadata: SesMetadata
    sql_text: str
    metadata_text: str
    dependencies: frozenset[SqlDependency]

    @property
    def ses_dependencies(self) -> frozenset[SqlDependency]:
        return frozenset(
            dependency for dependency in self.dependencies if len(dependency) == 2
        )

    @property
    def external_dependencies(self) -> frozenset[SqlDependency]:
        return frozenset(
            dependency for dependency in self.dependencies if len(dependency) == 3
        )


@dataclass(frozen=True)
class SesRepository:
    """A folder containing SES SQL files."""

    folder: Path

    def __init__(self, folder: str | Path):
        object.__setattr__(self, "folder", Path(folder))

    def iter_documents(self) -> tuple[SesSqlDocument, ...]:
        if not self.folder.is_dir():
            raise SesSyntaxException(f"SES repository folder does not exist: {self.folder}")
        documents = [
            read_ses_sql_file(path)
            for path in sorted(self.folder.rglob("*.sql"))
            if path.is_file()
        ]
        _validate_unique_repository_objects(documents)
        _validate_repository_dependencies(documents)
        return tuple(documents)

    def tables(self) -> tuple[SesSqlDocument, ...]:
        return tuple(
            document
            for document in self.iter_documents()
            if document.metadata.is_table
        )

    def views(self) -> tuple[SesSqlDocument, ...]:
        return tuple(
            document
            for document in self.iter_documents()
            if document.metadata.is_view
        )

    def get(self, qualified_name: str) -> SesSqlDocument:
        for document in self.iter_documents():
            if document.metadata.qualified_name == qualified_name:
                return document
        raise KeyError(f"SES object not found: {qualified_name}")

    def validated_dependencies(
        self,
        document_or_name: SesSqlDocument | str,
    ) -> frozenset[SqlDependency]:
        documents = self.iter_documents()
        document = _resolve_repository_document(documents, document_or_name)
        repository_names = _repository_object_names(documents)
        return frozenset(
            dependency
            for dependency in document.ses_dependencies
            if dependency in repository_names
        )

    def dependency_graph(self) -> dict[str, frozenset[SqlDependency]]:
        documents = self.iter_documents()
        return {
            document.metadata.qualified_name: self.validated_dependencies(document)
            for document in documents
        }


class _UniqueKeyLoader(yaml.SafeLoader):
    pass


def _construct_mapping_without_duplicate_keys(loader, node, deep=False):
    loader.flatten_mapping(node)
    mapping = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise SesValidationError(f"Duplicate metadata key: {key}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_mapping_without_duplicate_keys,
)


def read_ses_sql_file(path: str | Path) -> SesSqlDocument:
    """Read and parse an SES SQL file from disk."""

    sql_path = Path(path)
    document = parse_ses_sql(sql_path.read_text(encoding="utf-8"))
    expected_name = f"{document.metadata.qualified_name}.sql"
    if sql_path.name != expected_name:
        raise SesSyntaxException(
            f"SES file name must be {expected_name}, got {sql_path.name}"
        )
    return document


def parse_ses_sql(sql_text: str) -> SesSqlDocument:
    """Parse a SQL file whose leading block comment contains SES YAML metadata."""

    metadata_text, body_sql = _split_leading_metadata_block(sql_text)
    metadata = parse_ses_metadata(metadata_text)
    return SesSqlDocument(
        metadata=metadata,
        sql_text=body_sql,
        metadata_text=metadata_text,
        dependencies=find_sql_dependencies(body_sql),
    )


def parse_ses_metadata(metadata_text: str) -> SesMetadata:
    """Parse and validate SES metadata from the comment block body."""

    normalised_text = _normalise_metadata_yaml(metadata_text)
    try:
        loaded = yaml.load(normalised_text, Loader=_UniqueKeyLoader)
    except SesValidationError:
        raise
    except yaml.YAMLError as exc:
        raise SesValidationError(f"Invalid SES metadata YAML: {exc}") from exc

    if not isinstance(loaded, dict):
        raise SesValidationError("SES metadata must be a YAML mapping")

    return _metadata_from_mapping(loaded)


def _validate_unique_repository_objects(documents: list[SesSqlDocument]) -> None:
    seen: set[str] = set()
    for document in documents:
        qualified_name = document.metadata.qualified_name
        if qualified_name in seen:
            raise SesSyntaxException(
                f"Duplicate SES object in repository: {document.metadata.qualified_name}"
            )
        seen.add(qualified_name)


def _validate_repository_dependencies(documents: list[SesSqlDocument]) -> None:
    repository_names = _repository_object_names(documents)
    missing_dependencies = [
        f"{document.metadata.qualified_name} -> {format_sql_dependency(dependency)}"
        for document in documents
        for dependency in sorted(document.ses_dependencies)
        if dependency not in repository_names
    ]

    if missing_dependencies:
        raise SesSyntaxException(
            "Missing SES dependencies in repository: "
            + "; ".join(missing_dependencies)
        )


def _repository_object_names(
    documents: tuple[SesSqlDocument, ...] | list[SesSqlDocument],
) -> set[SqlDependency]:
    return {
        (document.metadata.schema, document.metadata.name)
        for document in documents
    }


def _resolve_repository_document(
    documents: tuple[SesSqlDocument, ...],
    document_or_name: SesSqlDocument | str,
) -> SesSqlDocument:
    if isinstance(document_or_name, SesSqlDocument):
        return document_or_name

    for document in documents:
        if document.metadata.qualified_name == document_or_name:
            return document

    raise KeyError(f"SES object not found: {document_or_name}")


def _split_leading_metadata_block(sql_text: str) -> tuple[str, str]:
    match = re.match(r"\s*/\*(.*?)\*/(.*)\Z", sql_text, flags=re.DOTALL)
    if not match:
        raise SesValidationError("SES SQL must begin with a /* ... */ metadata block")
    return match.group(1).strip("\n"), match.group(2).lstrip()


def _normalise_metadata_yaml(metadata_text: str) -> str:
    lines = metadata_text.splitlines()
    normalised_lines: list[str] = []
    current_block: str | None = None

    for line in lines:
        stripped = line.strip()
        indent = len(line) - len(line.lstrip(" "))

        top_level_match = re.match(r"^([A-Za-z][A-Za-z ]*):(?:\s+(.*))?$", stripped)
        if indent == 0 and top_level_match:
            current_block = top_level_match.group(1)
            key = current_block
            value = top_level_match.group(2)
            if key in {"Table ID", "View ID"} and value:
                normalised_lines.append(f"{key}: {_quote_yaml_scalar(value)}")
                continue

        if current_block == "Foreign keys" and stripped.startswith("- "):
            prefix = line[: line.index("- ") + 2]
            value = stripped[2:].strip()
            normalised_lines.append(f"{prefix}{_quote_yaml_scalar(value)}")
            continue

        normalised_lines.append(line)

    return "\n".join(normalised_lines)


def _quote_yaml_scalar(value: str) -> str:
    if not value:
        return value
    if value[:1] in {"'", '"'}:
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _metadata_from_mapping(raw: dict[str, Any]) -> SesMetadata:
    table_id = raw.get("Table ID")
    view_id = raw.get("View ID")
    if bool(table_id) == bool(view_id):
        raise SesValidationError("Exactly one of Table ID or View ID is required")

    object_kind = "table" if table_id else "view"
    object_id = _parse_object_id(str(table_id or view_id), f"{object_kind.title()} ID")
    description = _required_non_empty_string(raw, "Description")
    revision_notes = _parse_revision_notes(raw)

    primary_key = _parse_column_set(raw.get("Primary key"), "Primary key")
    identity = _optional_non_empty_string(raw.get("Identity"), "Identity")
    if identity and "," in identity:
        raise SesValidationError("Identity must be a single column name")
    unique_keys = _parse_column_set_list(raw.get("Unique keys"), "Unique keys")
    foreign_keys = _parse_foreign_keys(raw.get("Foreign keys"))
    column_notes = _parse_column_notes(raw.get("Column notes"))
    notes = _optional_non_empty_string(raw.get("Notes"), "Notes")

    return SesMetadata(
        object_kind=object_kind,
        object_id=object_id,
        description=description,
        revision_notes=revision_notes,
        primary_key=primary_key,
        identity=identity,
        unique_keys=unique_keys,
        foreign_keys=foreign_keys,
        column_notes=column_notes,
        notes=notes,
        raw=dict(raw),
    )


def _required_non_empty_string(raw: dict[str, Any], key: str) -> str:
    value = _optional_non_empty_string(raw.get(key), key)
    if value is None:
        raise SesValidationError(f"{key} is required")
    return value


def _optional_non_empty_string(value: Any, key: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise SesValidationError(f"{key} must be a string")
    stripped = value.strip()
    if not stripped:
        raise SesValidationError(f"{key} must not be empty")
    return stripped


def _parse_revision_notes(raw: dict[str, Any]) -> tuple[str, ...]:
    value = raw.get("Revision notes", raw.get("Revisions"))
    if value is None:
        raise SesValidationError("Revision notes are required")
    if not isinstance(value, list) or not value:
        raise SesValidationError("Revision notes must be a non-empty list")

    notes = []
    for item in value:
        if not isinstance(item, str):
            raise SesValidationError("Revision notes must be strings")
        stripped = item.strip()
        if not re.match(r"^(\d{4}-\d{2}-\d{2}|YYYY-MM-DD)\b", stripped):
            raise SesValidationError(
                "Each revision note must begin with YYYY-MM-DD"
            )
        notes.append(stripped)
    return tuple(notes)


def _parse_object_id(value: str, key: str) -> SesObjectId:
    parts = _split_schema_object(value)
    if len(parts) != 2 or not all(parts):
        raise SesValidationError(f"{key} must be in Schema.Name format")
    return SesObjectId(schema=parts[0], name=parts[1])


def _split_schema_object(value: str) -> list[str]:
    parts: list[str] = []
    current: list[str] = []
    bracket_depth = 0
    brace_depth = 0

    for character in value.strip():
        if character == "[":
            bracket_depth += 1
        elif character == "]" and bracket_depth:
            bracket_depth -= 1
        elif character == "{":
            brace_depth += 1
        elif character == "}" and brace_depth:
            brace_depth -= 1
        elif character == "." and bracket_depth == 0 and brace_depth == 0:
            parts.append(_normalise_identifier_part("".join(current)))
            current = []
            continue
        current.append(character)

    parts.append(_normalise_identifier_part("".join(current)))
    return parts


def _normalise_identifier_part(value: str) -> str:
    stripped = value.strip()
    if stripped.startswith("[") and stripped.endswith("]"):
        return stripped[1:-1].strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        return stripped[1:-1].strip()
    return stripped


def _parse_column_set(value: Any, key: str, *, required: bool = False) -> tuple[str, ...]:
    if value is None:
        if required:
            raise SesValidationError(f"{key} is required")
        return ()
    if isinstance(value, str):
        columns = tuple(column.strip() for column in value.split(","))
    elif isinstance(value, list):
        columns = tuple(str(column).strip() for column in value)
    else:
        raise SesValidationError(f"{key} must be a comma-separated column set")

    if not columns or any(not column for column in columns):
        raise SesValidationError(f"{key} must not contain empty column names")
    if len(set(columns)) != len(columns):
        raise SesValidationError(f"{key} must not contain duplicate column names")
    return columns


def _parse_column_set_list(value: Any, key: str) -> tuple[tuple[str, ...], ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise SesValidationError(f"{key} must be a list of column sets")
    return tuple(_parse_column_set(item, key, required=True) for item in value)


def _parse_foreign_keys(value: Any) -> tuple[SesForeignKey, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise SesValidationError("Foreign keys must be a list")
    return tuple(_parse_foreign_key(item) for item in value)


def _parse_foreign_key(value: Any) -> SesForeignKey:
    if not isinstance(value, str):
        raise SesValidationError("Foreign key entries must be strings")

    match = re.match(r"^(?P<child>.+?)\s*:\s*(?P<parent>.+?)\[(?P<parent_cols>.+)\]\s*$", value)
    if not match:
        raise SesValidationError(
            "Foreign key entries must be 'child columns: Schema.Table[parent columns]'"
        )

    child_columns = _parse_column_set(
        match.group("child"),
        "Foreign key child columns",
        required=True,
    )
    parent_columns = _parse_column_set(
        match.group("parent_cols"),
        "Foreign key parent columns",
        required=True,
    )
    if len(child_columns) != len(parent_columns):
        raise SesValidationError(
            "Foreign key child and parent column sets must have the same length"
        )
    return SesForeignKey(
        child_columns=child_columns,
        parent=_parse_object_id(match.group("parent"), "Foreign key parent"),
        parent_columns=parent_columns,
    )


def _parse_column_notes(value: Any) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise SesValidationError("Column notes must be a mapping of column name to note")

    notes: dict[str, str] = {}
    normalised_names: set[str] = set()
    for column_name, note in value.items():
        if not isinstance(column_name, str) or not column_name.strip():
            raise SesValidationError("Column note keys must be non-empty column names")
        if not isinstance(note, str):
            raise SesValidationError("Column notes must be strings")
        normalised_column_name = column_name.strip()
        lookup_name = normalised_column_name.lower()
        if lookup_name in normalised_names:
            raise SesValidationError(
                f"Duplicate column note for column: {normalised_column_name}"
            )
        normalised_names.add(lookup_name)
        notes[normalised_column_name] = note.strip()
    return notes
