"""Target-only load orchestrator.

Given an installed runtime root, the orchestrator:

1. Reads ``catalogue.json`` and ``load_dependency.json``.
2. Discovers installed object files structurally under ``objects/``.
3. Validates discovery against the catalogue and catalogue source hashes.
4. Selects only catalogue objects for the requested target by default.
5. Orders the selected set using dependency edges internal to that set.

It never reads the original SES repo and never requires ``--from``.
"""

from __future__ import annotations

from pathlib import Path

from ..build.manifest import read_json, source_hash
from ..errors import LoadError
from ..ses.graph import topological_order
from ..ses.discovery import discover_runtime_objects
from .logging import LoadReport, planned_step_log

CATALOGUE_NAME = "catalogue.json"
LOAD_DEPENDENCY_NAME = "load_dependency.json"
TABLE_DICTIONARY_NAME = "table_dictionary.json"
COLUMN_DICTIONARY_NAME = "column_dictionary.json"
INDEX_DICTIONARY_NAME = "index_dictionary.json"
FOREIGN_KEY_DICTIONARY_NAME = "foreign_key_dictionary.json"
MANIFEST_NAME = "manifest.json"


def load_target_runtime(
    runtime_root,
    *,
    execute: bool = True,
    object_filter: tuple[str, ...] | None = None,
    target_filter: str | None = None,
    include_static: bool = False,
    strict: bool = True,
    spark=None,
    spark_root=None,
) -> LoadReport:
    """Load a target from its installed runtime bundle."""

    root = Path(runtime_root)
    if not root.is_dir():
        raise LoadError(f"runtime root does not exist: {root}")

    catalogue = _read(root, CATALOGUE_NAME)
    load_dependency = _read(root, LOAD_DEPENDENCY_NAME)
    dictionaries = {
        "table": _read(root, TABLE_DICTIONARY_NAME),
        "column": _read(root, COLUMN_DICTIONARY_NAME),
        "index": _read(root, INDEX_DICTIONARY_NAME),
        "foreign_key": _read(root, FOREIGN_KEY_DICTIONARY_NAME),
        "manifest": _read_optional(root, MANIFEST_NAME, {}),
    }

    objects_root = root / "objects"
    discovered = {obj.id: obj for obj in discover_runtime_objects(objects_root)}
    _validate_against_catalogue(root, discovered, catalogue, strict=strict)

    steps = select_objects_for_target(
        catalogue,
        load_dependency,
        target_filter,
        object_filter=object_filter,
        include_static=include_static,
    )

    if not execute:
        return LoadReport(
            runtime_root=str(root),
            executed=False,
            ok=True,
            steps=tuple(
                planned_step_log(
                    step["object"],
                    step["kind"],
                    module=_module_name(discovered, step["object"]),
                )
                for step in steps
            ),
            message="validated installed runtime (not executed)",
        )

    # Execution requires Spark; imported lazily so core stays PySpark-free.
    from .load import execute_load_plan

    return execute_load_plan(
        runtime_root=root,
        catalogue=catalogue,
        dictionaries=dictionaries,
        discovered=discovered,
        steps=steps,
        include_static=include_static,
        spark=spark,
        spark_root=spark_root,
    )


def select_objects_for_target(
    catalogue: dict,
    load_dependency: dict,
    target_alias: str | None,
    *,
    object_filter: tuple[str, ...] | None,
    include_static: bool,
) -> list[dict]:
    """Select loadable catalogue objects and sort selected-internal edges only."""

    catalogue_by_id = {entry["id"]: entry for entry in catalogue.get("objects", [])}
    loadable = {
        object_id
        for object_id, entry in catalogue_by_id.items()
        if entry.get("kind") in {"Folder", "Table"}
    }
    if target_alias is None:
        selected = set(loadable)
    else:
        selected = {
            object_id
            for object_id, entry in catalogue_by_id.items()
            if entry.get("target_database") == target_alias and object_id in loadable
        }
    if object_filter is not None:
        selected = selected & set(object_filter)

    if not include_static:
        selected = {
            object_id
            for object_id in selected
            if not catalogue_by_id[object_id].get("static", False)
        }

    graph = load_dependency.get("objects", {})
    edges = [
        (dependency_id, object_id)
        for object_id in selected
        for dependency_id in graph.get(object_id, [])
        if dependency_id in selected
    ]
    try:
        ordered_ids = topological_order(selected, edges)
    except Exception as exc:
        raise LoadError(str(exc)) from exc

    return [
        {
            "object": object_id,
            "kind": catalogue_by_id[object_id]["kind"],
            "action": _action_for(catalogue_by_id[object_id]["kind"]),
        }
        for object_id in ordered_ids
    ]


def _action_for(kind: str) -> str:
    if kind == "Folder":
        return "run_read_and_sync"
    if kind == "Table":
        return "run_read_and_apply_policy"
    return "skip"


def _module_name(discovered, object_id: str) -> str:
    source_object = discovered.get(object_id)
    return Path(source_object.source_path).name if source_object is not None else ""


def _read(root: Path, name: str) -> dict:
    path = root / name
    if not path.is_file():
        raise LoadError(f"installed runtime is missing {name}: {path}")
    return read_json(path)


def _read_optional(root: Path, name: str, default: dict) -> dict:
    path = root / name
    if not path.is_file():
        return default
    return read_json(path)


def _validate_against_catalogue(root: Path, discovered, catalogue, *, strict: bool) -> None:
    catalogue_by_id = {entry["id"]: entry for entry in catalogue.get("objects", [])}
    catalogue_ids = set(catalogue_by_id)

    missing = sorted(catalogue_ids - set(discovered))
    if missing:
        raise LoadError(
            "installed runtime is missing catalogue objects: " + ", ".join(missing)
        )

    unknown = sorted(set(discovered) - catalogue_ids)
    if unknown and strict:
        raise LoadError(
            "installed runtime has objects not in the catalogue: " + ", ".join(unknown)
        )

    for object_id in sorted(catalogue_ids):
        entry = catalogue_by_id[object_id]
        expected = entry.get("source_hash")
        if expected is None:
            raise LoadError(f"source hash missing for {object_id}")
        installed_source = entry.get("installed_source")
        if not installed_source:
            raise LoadError(f"installed source path missing for {object_id}")
        source_path = root / installed_source
        if not source_path.is_file():
            raise LoadError(f"installed source is missing for {object_id}: {source_path}")
        actual = source_hash(source_path.read_text(encoding="utf-8"))
        if actual != expected:
            raise LoadError(
                f"source hash mismatch for {object_id}: installed runtime does not "
                "match recorded source"
            )
