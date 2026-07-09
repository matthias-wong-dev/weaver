"""Dependency classification.

A reference is classified relative to the object's current database and the set
of database names supplied to the build:

* two-part ``Schema.Object``               -> intra-database
* three-part ``Db.Schema.Object``          -> intra if Db is the current db,
                                              managed cross-database if Db is
                                              supplied, otherwise external
* four-part ``Server.Db.Schema.Object``    -> external

External references are recorded but never built, initialised, pruned, or
loaded.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .discovery import PYTHON, SourceObject
from .python_discovery import extract_python_references
from .sql_discovery import extract_sql_references

INTRA = "intra_database"
MANAGED_CROSS = "managed_cross_database"
EXTERNAL = "external"


@dataclass(frozen=True)
class Dependency:
    """A classified dependency of one object on another."""

    id: str
    scope: str
    reference: str

    @property
    def is_intra(self) -> bool:
        return self.scope == INTRA

    @property
    def is_managed_cross(self) -> bool:
        return self.scope == MANAGED_CROSS

    @property
    def is_external(self) -> bool:
        return self.scope == EXTERNAL

    @property
    def is_managed(self) -> bool:
        return self.scope in (INTRA, MANAGED_CROSS)


def classify_reference(
    parts: tuple[str, ...],
    current_database: str,
    managed_databases: Iterable[str],
) -> Dependency:
    """Classify a single multi-part reference."""

    managed = set(managed_databases)
    reference = ".".join(parts)

    if len(parts) == 2:
        schema, object_name = parts
        return Dependency(
            id=f"{current_database}.{schema}.{object_name}",
            scope=INTRA,
            reference=reference,
        )

    if len(parts) == 3:
        database, schema, object_name = parts
        target_id = f"{database}.{schema}.{object_name}"
        if database == current_database:
            return Dependency(id=target_id, scope=INTRA, reference=reference)
        scope = MANAGED_CROSS if database in managed else EXTERNAL
        return Dependency(id=target_id, scope=scope, reference=reference)

    return Dependency(id=reference, scope=EXTERNAL, reference=reference)


def raw_references(source_object: SourceObject) -> tuple[tuple[str, ...], ...]:
    """Extract raw multi-part references from an object's body."""

    if source_object.language == PYTHON:
        return extract_python_references(source_object.text)
    return extract_sql_references(source_object.sql_body or "")


def classify_object_dependencies(
    source_object: SourceObject,
    managed_databases: Iterable[str],
) -> tuple[Dependency, ...]:
    """Classify every dependency of an object, dropping self-references."""

    managed = set(managed_databases)
    dependencies: list[Dependency] = []
    seen: set[str] = set()
    for parts in raw_references(source_object):
        dependency = classify_reference(parts, source_object.database, managed)
        if dependency.id == source_object.id:
            continue
        if dependency.id not in seen:
            seen.add(dependency.id)
            dependencies.append(dependency)
    return tuple(dependencies)
