"""Adapter and object-kind compatibility rules.

* Files targets accept Folder objects only.
* Delta targets accept Table objects only.
* SQL targets accept Table and View objects.
* Sources must be SES; targets must be Files/Delta/SQL.
"""

from __future__ import annotations

from ..config.databases import DELTA, FILES, SES, SQL
from ..errors import CompatibilityError
from ..ses.metadata import FOLDER, TABLE, VIEW

_KIND_BY_TARGET = {
    FILES: frozenset({FOLDER}),
    DELTA: frozenset({TABLE}),
    SQL: frozenset({TABLE, VIEW}),
}

_TARGET_TYPES = frozenset(_KIND_BY_TARGET)


def validate_pair(source_type: str, target_type: str) -> None:
    """Validate that a from/to pair is adapter-compatible."""

    if source_type != SES:
        raise CompatibilityError(
            f"source type {source_type!r} cannot be built from; sources must be SES"
        )
    if target_type not in _TARGET_TYPES:
        raise CompatibilityError(
            f"target type {target_type!r} is not a supported build target"
        )


def validate_object_kind(kind: str, target_type: str) -> None:
    """Validate that an object kind can install to a target type."""

    allowed = _KIND_BY_TARGET.get(target_type)
    if allowed is None:
        raise CompatibilityError(f"target type {target_type!r} is not supported")
    if kind not in allowed:
        raise CompatibilityError(
            f"{kind} objects cannot install to a {target_type} target"
        )
