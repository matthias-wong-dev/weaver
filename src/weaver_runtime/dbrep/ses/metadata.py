"""Object metadata parsing and normalisation.

Metadata lives in a Python module docstring or a leading SQL ``/* ... */``
block, written as a small YAML mapping::

    Table ID: Stage.Record
    Description: Normalised records.
    Lineage: Reads raw records and creates a typed table.
    Primary key: record_id
    Auto delete: false

Declarations are two-part ``Schema.Object``; Weaver normalises the database from
the containing representation. Exactly one of ``Folder ID`` / ``Table ID`` /
``View ID`` must be present.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from typing import Any

import yaml

from ..errors import MetadataError

FOLDER = "Folder"
TABLE = "Table"
VIEW = "View"
OBJECT_KINDS = frozenset({FOLDER, TABLE, VIEW})

APPEND = "append"
REPLACE = "replace"
UPSERT = "upsert"
LOAD_MODES = frozenset({APPEND, UPSERT})

_ID_KEYS = {"Folder ID": FOLDER, "Table ID": TABLE, "View ID": VIEW}
_PLACEHOLDERS = {"not declared", "n/a", "tbd", "todo"}


@dataclass(frozen=True)
class ObjectId:
    """Two-part object identity within a database representation."""

    schema: str
    object: str

    @property
    def qualified(self) -> str:
        return f"{self.schema}.{self.object}"


@dataclass(frozen=True)
class ObjectMetadata:
    """Validated metadata for a single Weaver object."""

    kind: str
    object_id: ObjectId
    description: str
    lineage: str
    primary_key: tuple[str, ...]
    file_keys: tuple[str, ...]
    auto_delete: bool
    static: bool
    load_mode: str | None
    schema: tuple[tuple[str, str], ...]
    raw: dict[str, Any]

    @property
    def qualified(self) -> str:
        return self.object_id.qualified

    @property
    def has_primary_key(self) -> bool:
        return bool(self.primary_key)

    @property
    def effective_load_mode(self) -> str:
        """Load mode after applying defaults: PK implies upsert, else replace."""

        if self.load_mode is not None:
            return self.load_mode
        if self.kind == TABLE:
            return UPSERT if self.has_primary_key else REPLACE
        return APPEND


class _UniqueKeyLoader(yaml.SafeLoader):
    """YAML loader that rejects duplicate mapping keys."""


def _no_duplicate_keys(loader, node, deep=False):
    loader.flatten_mapping(node)
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise MetadataError(f"duplicate metadata key: {key}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _no_duplicate_keys,
)


def extract_python_metadata_text(source: str) -> str:
    """Return the metadata YAML text from a Python object file's docstring."""

    try:
        module = ast.parse(source)
    except SyntaxError as exc:
        raise MetadataError(f"python object file is not parseable: {exc}") from exc
    doc = ast.get_docstring(module, clean=True)
    if doc is None or not doc.strip():
        raise MetadataError(
            "python object file must begin with a docstring metadata block"
        )
    return doc


def extract_sql_metadata_and_body(source: str) -> tuple[str, str]:
    """Split a SQL object file into (metadata text, executable SQL body)."""

    match = re.match(r"\s*/\*(.*?)\*/(.*)\Z", source, flags=re.DOTALL)
    if not match:
        raise MetadataError("SES SQL must begin with a /* ... */ metadata block")
    return match.group(1).strip("\n"), match.group(2).lstrip()


def parse_object_metadata(text: str) -> ObjectMetadata:
    """Parse and validate an object metadata block."""

    try:
        loaded = yaml.load(text, Loader=_UniqueKeyLoader)
    except MetadataError:
        raise
    except yaml.YAMLError as exc:
        raise MetadataError(f"invalid metadata YAML: {exc}") from exc

    if not isinstance(loaded, dict):
        raise MetadataError("metadata must be a YAML mapping")

    kind, object_id = _parse_id(loaded)
    description = _required_text(loaded, "Description")
    lineage = _required_text(loaded, "Lineage")
    primary_key = _parse_primary_key(loaded.get("Primary key"))
    file_keys = _parse_file_keys(
        loaded.get("File key"), kind=kind, declared="File key" in loaded
    )
    auto_delete = _parse_auto_delete(
        loaded.get("Auto delete"),
        kind=kind,
        has_primary_key=bool(primary_key),
    )
    static = _parse_bool(loaded.get("Static"), "Static")
    load_mode = _parse_load_mode(loaded.get("Load mode"))
    schema = _parse_schema(loaded.get("Schema"))

    if kind == TABLE and auto_delete and not primary_key:
        raise MetadataError(
            "Auto delete requires a Primary key (no-PK auto-delete is invalid)"
        )

    return ObjectMetadata(
        kind=kind,
        object_id=object_id,
        description=description,
        lineage=lineage,
        primary_key=primary_key,
        file_keys=file_keys,
        auto_delete=auto_delete,
        static=static,
        load_mode=load_mode,
        schema=schema,
        raw=dict(loaded),
    )


def _parse_id(raw: dict[str, Any]) -> tuple[str, ObjectId]:
    present = [key for key in _ID_KEYS if key in raw and raw[key] is not None]
    if len(present) != 1:
        raise MetadataError(
            "metadata must include exactly one of Folder ID, Table ID, View ID"
        )
    key = present[0]
    value = raw[key]
    if not isinstance(value, str) or not value.strip():
        raise MetadataError(f"{key} must be a non-empty Schema.Object string")
    parts = [part.strip() for part in value.strip().split(".")]
    if len(parts) != 2 or not all(parts):
        raise MetadataError(
            f"{key} must be a two-part Schema.Object declaration, got {value!r}"
        )
    return _ID_KEYS[key], ObjectId(schema=parts[0], object=parts[1])


def _required_text(raw: dict[str, Any], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise MetadataError(f"{key} is required and must be non-empty text")
    stripped = value.strip()
    if stripped.lower() in _PLACEHOLDERS:
        raise MetadataError(f"{key} must not be a placeholder value ({stripped!r})")
    return stripped


def _parse_primary_key(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, list):
        raise MetadataError("Primary key must be scalar text, not a YAML list")
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise MetadataError("Primary key must be scalar text")
    columns = tuple(part.strip() for part in str(value).split(","))
    if any(not column for column in columns):
        raise MetadataError("Primary key must not contain empty column names")
    if len(set(columns)) != len(columns):
        raise MetadataError("Primary key must not repeat columns")
    return columns


def _parse_file_keys(value: Any, *, kind: str, declared: bool) -> tuple[str, ...]:
    if kind != FOLDER:
        if declared:
            raise MetadataError("File key is supported only for Folder objects")
        return ()
    if value is None:
        raise MetadataError("Folder metadata must declare File key")

    values = [value] if isinstance(value, str) else value
    if not isinstance(values, list) or not values:
        raise MetadataError("File key must be a non-empty string or list of strings")

    patterns: list[str] = []
    for pattern in values:
        if not isinstance(pattern, str) or not pattern.strip():
            raise MetadataError("File key patterns must be non-empty strings")
        normalised = pattern.strip().replace("\\", "/")
        path_parts = normalised.split("/")
        if normalised.startswith("/") or ".." in path_parts:
            raise MetadataError(
                "File key patterns must be relative and must not traverse with '..'"
            )
        patterns.append(normalised)
    return tuple(patterns)


def _parse_bool(value: Any, key: str) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    raise MetadataError(f"{key} must be a boolean (true/false)")


def _parse_auto_delete(value: Any, *, kind: str, has_primary_key: bool) -> bool:
    """Apply object-aware defaults while preserving explicit declarations."""

    if value is None:
        return kind == TABLE and has_primary_key
    return _parse_bool(value, "Auto delete")


def _parse_load_mode(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or value.strip().lower() not in LOAD_MODES:
        raise MetadataError(
            f"Load mode must be one of {', '.join(sorted(LOAD_MODES))} when provided"
        )
    return value.strip().lower()


def _parse_schema(value: Any) -> tuple[tuple[str, str], ...]:
    if value is None:
        return ()
    if not isinstance(value, dict) or not value:
        raise MetadataError("Schema must be a non-empty mapping of column to type")
    columns: list[tuple[str, str]] = []
    for column, column_type in value.items():
        if not isinstance(column, str) or not column.strip():
            raise MetadataError("Schema column names must be non-empty strings")
        if not isinstance(column_type, str) or not column_type.strip():
            raise MetadataError("Schema column types must be non-empty strings")
        columns.append((column.strip(), column_type.strip()))
    return tuple(columns)
