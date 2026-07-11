"""Structural discovery of database folders and object files.

The same discoverable structure is used by the source SES repo and the installed
runtime bundle:

* Immediate child folders of a root that do not begin ``_`` are database folders.
* Any child beginning ``_`` (``_orchestrator``, ``_helpers``, ``_shared`` ...)
  is ignored by object discovery.
* Python/SQL files immediately under a database folder are object files unless
  the filename begins ``_``.
* Helper folders/files are importable but never discovered as objects.

Object modules are never imported here: metadata is read statically.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

from ..errors import DiscoveryError
from .metadata import (
    FOLDER,
    TABLE,
    VIEW,
    ObjectMetadata,
    extract_python_metadata_text,
    extract_sql_metadata_and_body,
    parse_object_metadata,
)

PYTHON = "python"
SQL = "sql"
_OBJECT_SUFFIXES = {".py": PYTHON, ".sql": SQL}

# Weaver object base classes recognised statically. Bases are matched by name
# (``Table``/``Folder``/``View``) whether imported plainly or via attribute
# access (``objects.Table``). A base reached only through an aliased import
# (``from ... import Table as T``) is deliberately NOT recognised: discovery
# never imports the module, so it cannot resolve aliases.
_WEAVER_BASES = frozenset({FOLDER, TABLE, VIEW})


@dataclass(frozen=True)
class SourceObject:
    """A discovered object file bound to its database representation."""

    database: str
    metadata: ObjectMetadata
    language: str
    source_path: Path
    text: str
    sql_body: str | None

    @property
    def declared_as(self) -> str:
        return self.metadata.qualified

    @property
    def id(self) -> str:
        """Normalised three-part identity: ``Database.Schema.Object``."""

        return f"{self.database}.{self.metadata.qualified}"

    @property
    def kind(self) -> str:
        return self.metadata.kind


def is_ignored(name: str) -> bool:
    """Structural ignore rule: names beginning with ``_``."""

    return name.startswith("_")


def discover_database_folders(root: Path) -> list[Path]:
    """Immediate child folders that are database folders (not ``_``-prefixed)."""

    root = Path(root)
    if not root.is_dir():
        raise DiscoveryError(f"discovery root does not exist: {root}")
    return sorted(
        child
        for child in root.iterdir()
        if child.is_dir() and not is_ignored(child.name)
    )


def discover_object_files(database_folder: Path) -> list[Path]:
    """Immediate object files under a database folder (not ``_``-prefixed)."""

    database_folder = Path(database_folder)
    if not database_folder.is_dir():
        raise DiscoveryError(f"database folder does not exist: {database_folder}")
    return sorted(
        child
        for child in database_folder.iterdir()
        if child.is_file()
        and child.suffix in _OBJECT_SUFFIXES
        and not is_ignored(child.name)
    )


def load_source_object(path: Path, database: str) -> SourceObject:
    """Parse a single object file into a :class:`SourceObject`."""

    path = Path(path)
    language = _OBJECT_SUFFIXES.get(path.suffix)
    if language is None:
        raise DiscoveryError(f"not an object file: {path}")

    text = path.read_text(encoding="utf-8")
    if language == PYTHON:
        metadata = parse_object_metadata(extract_python_metadata_text(text))
        sql_body = None
        expected_stem = metadata.qualified.replace(".", "__")
    else:
        metadata_text, sql_body = extract_sql_metadata_and_body(text)
        metadata = parse_object_metadata(metadata_text)
        expected_stem = metadata.qualified

    if path.stem != expected_stem:
        raise DiscoveryError(
            f"object file {path.name} must be named for its declared object "
            f"{metadata.qualified} (expected stem {expected_stem!r})"
        )

    if language == PYTHON:
        _validate_python_object_class(text, path, expected_stem)

    return SourceObject(
        database=database,
        metadata=metadata,
        language=language,
        source_path=path,
        text=text,
        sql_body=sql_body,
    )


def _weaver_base_name(base: ast.expr) -> str | None:
    """Name of a Weaver base class (``Table``/``Folder``/``View``), or ``None``."""

    if isinstance(base, ast.Name) and base.id in _WEAVER_BASES:
        return base.id
    if isinstance(base, ast.Attribute) and base.attr in _WEAVER_BASES:
        return base.attr
    return None


def _validate_python_object_class(text: str, path: Path, expected_stem: str) -> None:
    """Require exactly one Weaver object class named for the filename stem.

    Static (``ast``) check — the module is never imported. Helper classes that do
    not derive from a Weaver base may coexist. Raises :class:`DiscoveryError`
    naming the source, the expected class, and the Weaver classes found.
    """

    try:
        module = ast.parse(text)
    except SyntaxError as exc:
        raise DiscoveryError(f"python object file {path} is not parseable: {exc}") from exc

    weaver_classes = [
        node.name
        for node in module.body
        if isinstance(node, ast.ClassDef)
        and any(_weaver_base_name(base) is not None for base in node.bases)
    ]

    if len(weaver_classes) != 1:
        found = ", ".join(weaver_classes) if weaver_classes else "none"
        raise DiscoveryError(
            f"python object {path.name} must define exactly one Weaver object class "
            f"(subclass of Folder/Table/View) named {expected_stem!r}; found {found}"
        )
    if weaver_classes[0] != expected_stem:
        raise DiscoveryError(
            f"python object class in {path.name} must be named {expected_stem!r} to "
            f"match the declared object; found {weaver_classes[0]!r}"
        )


def discover_database(database_folder: Path, database: str) -> tuple[SourceObject, ...]:
    """Discover every object in a single database folder."""

    objects = [
        load_source_object(path, database)
        for path in discover_object_files(database_folder)
    ]
    _validate_unique(objects, database)
    return tuple(objects)


def discover_runtime_objects(runtime_root: Path) -> tuple[SourceObject, ...]:
    """Discover every object across all database folders under a runtime root."""

    objects: list[SourceObject] = []
    for database_folder in discover_database_folders(runtime_root):
        objects.extend(discover_database(database_folder, database_folder.name))
    return tuple(objects)


def _validate_unique(objects: list[SourceObject], database: str) -> None:
    seen: set[str] = set()
    for source_object in objects:
        declared = source_object.declared_as
        if declared in seen:
            raise DiscoveryError(
                f"duplicate object {declared!r} declared in database {database!r}"
            )
        seen.add(declared)
