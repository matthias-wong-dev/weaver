"""Runtime metadata artifacts installed under ``Files/_weaver/runtime``.

The runtime catalogue is the load-time object inventory and hash store. The
dependency graph drives target-selected load ordering. Dictionary artifacts are
documentation-oriented metadata snapshots for the installed objects.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Sequence

from ..config.resolution import RUNTIME_RELATIVE_ROOT
from ..ses.metadata import FOLDER, TABLE

FOLDER_ACTION = "run_load"
TABLE_ACTION = "run_read_and_apply_policy"


def weaver_version() -> str:
    try:
        from importlib.metadata import version

        return version("weaver-runtime")
    except Exception:  # pragma: no cover - metadata always present when installed
        return "0+unknown"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def source_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def build_manifest(
    objects: Sequence,
    *,
    target_server: str,
    installed_from: Iterable[str],
    installed_to: Iterable[str],
    external_dependencies: Iterable,
    installed_at: str | None = None,
    version: str | None = None,
    runtime_root: str = RUNTIME_RELATIVE_ROOT,
) -> dict:
    """Build the optional provenance manifest for one runtime host."""

    return {
        "version": 1,
        "target_server": target_server,
        "runtime_root": runtime_root,
        "built_at": installed_at or utc_now_iso(),
        "weaver_version": version or weaver_version(),
        "installed_from": sorted(set(installed_from)),
        "installed_to": sorted(set(installed_to)),
        "object_count": len(objects),
        "external_dependencies": [
            {"id": external.id, "reason": external.reason}
            for external in external_dependencies
        ],
    }


def build_catalogue(objects: Sequence) -> dict:
    """Build the installed object catalogue and source hash store."""

    return {
        "version": 1,
        "objects": [_catalogue_object(planned) for planned in objects],
    }


def _catalogue_object(planned) -> dict:
    metadata = planned.source.metadata
    return {
        "id": planned.id,
        "declared_as": planned.declared_as,
        "kind": planned.kind,
        "source_database": planned.source_alias,
        "source_database_name": planned.source_database,
        "target_database": planned.target_alias,
        "target_database_name": planned.target_database,
        "materialisation": planned.materialisation,
        "installed_source": _installed_source(planned),
        "source_hash": source_hash(planned.source.text),
        "static": metadata.static,
        "language": planned.source.language,
    }


def _installed_source(planned) -> str:
    return f"objects/{planned.source_database}/{planned.source.source_path.name}"


def build_load_dependency(objects: Sequence) -> dict:
    """Build the installed dependency graph for load ordering."""

    return {
        "version": 1,
        "objects": {
            planned.id: [dependency.id for dependency in planned.dependencies]
            for planned in objects
        },
    }


def build_table_dictionary(objects: Sequence) -> dict:
    """Build object/table/view/folder descriptive metadata."""

    tables = []
    for planned in objects:
        metadata = planned.source.metadata
        tables.append(
            {
                "id": planned.id,
                "kind": planned.kind,
                "description": metadata.description,
                "lineage": metadata.lineage,
                "notes": _string_metadata(metadata.raw.get("Notes")),
                "static": metadata.static,
                "load_mode": metadata.effective_load_mode,
                "is_incremental": metadata.is_incremental,
            }
        )
    return {"version": 1, "tables": tables}


def build_column_dictionary(objects: Sequence) -> dict:
    """Build column-level metadata and declared Spark/Delta type strings."""

    columns = []
    for planned in objects:
        metadata = planned.source.metadata
        notes = _mapping_metadata(metadata.raw.get("Column notes"))
        for ordinal, (column, type_name) in enumerate(metadata.schema, start=1):
            columns.append(
                {
                    "object_id": planned.id,
                    "ordinal": ordinal,
                    "column": column,
                    "type": type_name,
                    "description": _string_metadata(notes.get(column)),
                }
            )
    return {"version": 1, "columns": columns}


def build_index_dictionary(objects: Sequence) -> dict:
    """Build primary/unique key metadata."""

    indexes = []
    for planned in objects:
        primary_key = list(planned.source.metadata.primary_key)
        if not primary_key:
            continue
        indexes.append(
            {
                "object_id": planned.id,
                "index_name": "PK_" + planned.id.replace(".", "_"),
                "index_type": "primary_key",
                "columns": primary_key,
            }
        )
    return {"version": 1, "indexes": indexes}


def build_foreign_key_dictionary(objects: Sequence) -> dict:
    """Build declared semantic foreign key metadata where present."""

    foreign_keys = []
    for planned in objects:
        for item in _foreign_key_items(planned.source.metadata.raw.get("Foreign keys")):
            columns = _list_metadata(item.get("columns") or item.get("Columns"))
            referenced_columns = _list_metadata(
                item.get("referenced_columns") or item.get("Referenced columns")
            )
            referenced = item.get("referenced_object_id") or item.get("Referenced object")
            if not columns or not referenced or not referenced_columns:
                continue
            foreign_keys.append(
                {
                    "object_id": planned.id,
                    "foreign_key_name": _string_metadata(
                        item.get("foreign_key_name")
                        or item.get("Foreign key name")
                        or "FK_" + planned.id.replace(".", "_")
                    ),
                    "columns": columns,
                    "referenced_object_id": _string_metadata(referenced),
                    "referenced_columns": referenced_columns,
                    "description": _string_metadata(item.get("description") or item.get("Description")),
                }
            )
    return {"version": 1, "foreign_keys": foreign_keys}


def build_load_plan(
    objects_in_order: Sequence,
    *,
    server: str,
    targets: Iterable[str],
    runtime_root: str = RUNTIME_RELATIVE_ROOT,
) -> dict:
    """Compatibility builder for legacy callers; not installed as source of truth."""

    steps = []
    for planned in objects_in_order:
        if planned.kind == FOLDER:
            action = FOLDER_ACTION
        elif planned.kind == TABLE:
            action = TABLE_ACTION
        else:
            continue
        steps.append({"object": planned.id, "kind": planned.kind, "action": action})

    return {
        "server": server,
        "targets": sorted(set(targets)),
        "runtime_root": runtime_root,
        "steps": steps,
    }


def build_source_hashes(objects: Sequence) -> dict:
    """Compatibility builder for legacy callers; source hashes live in catalogue."""

    return {planned.id: source_hash(planned.source.text) for planned in objects}


def _string_metadata(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def _mapping_metadata(value) -> dict:
    return value if isinstance(value, dict) else {}


def _list_metadata(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [_string_metadata(item) for item in value if _string_metadata(item)]
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    return [_string_metadata(value)]


def _foreign_key_items(value) -> list[dict]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        return [value]
    return []


def write_json(path: Path, document: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")


def read_json(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))
