"""Install the runtime bundle and artifacts under a Lakehouse host.

For each Lakehouse host among the targets, this copies a self-contained
orchestrator, snapshots the supplied source databases (preserving layout,
including ``_helpers``), applies Files folder installs, and writes catalogue,
dependency, dictionary, and provenance metadata under ``Files/_weaver/runtime``.
SQL targets are recorded as plan-only installs.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path

from ..config.resolution import lakehouse_root, runtime_root, ses_source_root
from ..targets import InstallAction, get_adapter
from .manifest import (
    RUNTIME_RELATIVE_ROOT,
    build_catalogue,
    build_column_dictionary,
    build_foreign_key_dictionary,
    build_index_dictionary,
    build_load_dependency,
    build_manifest,
    build_table_dictionary,
    read_json,
    write_json,
)
from .planner import BuildPlan
from .prune import PreviousObject, PruneItem, plan_prune

DBREP_DIR = Path(__file__).resolve().parents[1]
_ENTRYPOINT = '''"""Installed Weaver load entrypoint."""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from weaver_runtime.dbrep.runtime.orchestrator import load_target_runtime


def main(runtime_root=None, execute=True):
    root = Path(runtime_root) if runtime_root else _HERE.parent
    return load_target_runtime(root, execute=execute)


if __name__ == "__main__":
    report = main()
    raise SystemExit(0 if report.ok else 1)
'''

_IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store")
_LEGACY_ARTIFACTS = ("load_plan.json", "source_hashes.json")


@dataclass
class HostInstall:
    """Result of installing one Lakehouse host's runtime bundle."""

    server: str
    runtime_root: str
    installed_objects: tuple[str, ...]
    files_created: tuple[str, ...]
    manifest_path: str
    catalogue_path: str
    load_dependency_path: str
    table_dictionary_path: str
    column_dictionary_path: str
    index_dictionary_path: str
    foreign_key_dictionary_path: str
    manifest: dict = field(default_factory=dict)
    catalogue: dict = field(default_factory=dict)
    load_dependency: dict = field(default_factory=dict)


@dataclass
class SqlInstall:
    """Plan-only SQL install actions for a SQL target."""

    target: str
    server: str
    database: str
    degrees_of_parallelism: int | None
    load_procedure: str
    actions: tuple[InstallAction, ...]


@dataclass
class InstallResult:
    hosts: tuple[HostInstall, ...]
    sql: tuple[SqlInstall, ...]
    pruned: tuple[PruneItem, ...]


def install_build(
    plan: BuildPlan,
    *,
    previous: list[PreviousObject] | None = None,
    installed_at: str | None = None,
) -> InstallResult:
    """Install a build plan to disk and return what was installed."""

    target_by_alias = {pair.target.alias: pair.target for pair in plan.pairs}
    source_by_alias = {pair.source.alias: pair.source for pair in plan.pairs}

    pruned = _apply_prune(plan, previous, target_by_alias)

    hosts = _install_hosts(plan, target_by_alias, source_by_alias, installed_at)
    sql = _install_sql_targets(plan, target_by_alias)

    return InstallResult(hosts=tuple(hosts), sql=tuple(sql), pruned=pruned)


def _install_hosts(plan, target_by_alias, source_by_alias, installed_at) -> list[HostInstall]:
    lakehouse_targets = {
        alias: resolved
        for alias, resolved in target_by_alias.items()
        if resolved.is_lakehouse
    }
    by_server: dict[str, list] = {}
    for resolved in lakehouse_targets.values():
        by_server.setdefault(resolved.server_alias, []).append(resolved)

    installs: list[HostInstall] = []
    for server_alias, resolved_targets in by_server.items():
        representative = resolved_targets[0]
        target_aliases = {resolved.alias for resolved in resolved_targets}
        host_objects = [o for o in plan.objects if o.target_alias in target_aliases]
        if not host_objects:
            continue
        installs.append(
            _install_one_host(
                representative,
                host_objects,
                source_by_alias,
                plan.external_dependencies,
                installed_at,
                prune=plan.prune,
            )
        )
    return installs


def _install_one_host(
    representative,
    host_objects,
    source_by_alias,
    external,
    installed_at,
    *,
    prune: bool,
) -> HostInstall:
    root = runtime_root(representative)
    host_root = lakehouse_root(representative)
    root.mkdir(parents=True, exist_ok=True)

    _install_orchestrator(root)
    _remove_legacy_artifacts(root)

    source_by_db = {}
    for planned in host_objects:
        source = source_by_alias[planned.source_alias]
        source_by_db[source.database] = source
    _install_sources(root, source_by_db)

    files_created = _apply_object_installs(host_objects, host_root)

    manifest = build_manifest(
        host_objects,
        target_server=representative.server_alias,
        installed_from={o.source_alias for o in host_objects},
        installed_to={o.target_alias for o in host_objects},
        external_dependencies=external,
        installed_at=installed_at,
    )
    source_aliases = {o.source_alias for o in host_objects}
    target_aliases = {o.target_alias for o in host_objects}
    metadata_docs, pruned_entries = _merged_runtime_metadata(
        root,
        catalogue=build_catalogue(host_objects),
        load_dependency=build_load_dependency(host_objects),
        table_dictionary=build_table_dictionary(host_objects),
        column_dictionary=build_column_dictionary(host_objects),
        index_dictionary=build_index_dictionary(host_objects),
        foreign_key_dictionary=build_foreign_key_dictionary(host_objects),
        source_aliases=source_aliases,
        target_aliases=target_aliases,
        prune=prune,
    )
    _remove_pruned_sources(root, metadata_docs["catalogue"], pruned_entries)

    manifest_path = root / "manifest.json"
    catalogue_path = root / "catalogue.json"
    load_dependency_path = root / "load_dependency.json"
    table_dictionary_path = root / "table_dictionary.json"
    column_dictionary_path = root / "column_dictionary.json"
    index_dictionary_path = root / "index_dictionary.json"
    foreign_key_dictionary_path = root / "foreign_key_dictionary.json"
    write_json(manifest_path, manifest)
    write_json(catalogue_path, metadata_docs["catalogue"])
    write_json(load_dependency_path, metadata_docs["load_dependency"])
    write_json(table_dictionary_path, metadata_docs["table_dictionary"])
    write_json(column_dictionary_path, metadata_docs["column_dictionary"])
    write_json(index_dictionary_path, metadata_docs["index_dictionary"])
    write_json(foreign_key_dictionary_path, metadata_docs["foreign_key_dictionary"])

    return HostInstall(
        server=representative.server_alias,
        runtime_root=str(root),
        installed_objects=tuple(o.id for o in host_objects),
        files_created=files_created,
        manifest_path=str(manifest_path),
        catalogue_path=str(catalogue_path),
        load_dependency_path=str(load_dependency_path),
        table_dictionary_path=str(table_dictionary_path),
        column_dictionary_path=str(column_dictionary_path),
        index_dictionary_path=str(index_dictionary_path),
        foreign_key_dictionary_path=str(foreign_key_dictionary_path),
        manifest=manifest,
        catalogue=metadata_docs["catalogue"],
        load_dependency=metadata_docs["load_dependency"],
    )


def _install_orchestrator(root: Path) -> None:
    orchestrator = root / "_orchestrator"
    if orchestrator.exists():
        shutil.rmtree(orchestrator)
    package = orchestrator / "weaver_runtime"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text('"""Bundled Weaver runtime."""\n', encoding="utf-8")
    shutil.copytree(DBREP_DIR, package / "dbrep", ignore=_IGNORE)
    (orchestrator / "weaver_load.py").write_text(_ENTRYPOINT, encoding="utf-8")


def _remove_legacy_artifacts(root: Path) -> None:
    for name in _LEGACY_ARTIFACTS:
        path = root / name
        if path.exists():
            path.unlink()


def _install_sources(root: Path, source_by_db: dict) -> None:
    objects_root = root / "objects"
    objects_root.mkdir(parents=True, exist_ok=True)

    for database, source in source_by_db.items():
        source_root = ses_source_root(source)
        legacy_destination = root / database
        if legacy_destination.exists():
            shutil.rmtree(legacy_destination)
        destination = objects_root / database
        shutil.copytree(source_root, destination, dirs_exist_ok=True, ignore=_IGNORE)

        shared_helpers = source_root.parent / "_helpers"
        if shared_helpers.is_dir():
            shutil.copytree(
                shared_helpers,
                objects_root / "_helpers",
                dirs_exist_ok=True,
                ignore=_IGNORE,
            )

    legacy_helpers = root / "_helpers"
    if legacy_helpers.exists():
        shutil.rmtree(legacy_helpers)


def _merged_runtime_metadata(
    root: Path,
    *,
    catalogue: dict,
    load_dependency: dict,
    table_dictionary: dict,
    column_dictionary: dict,
    index_dictionary: dict,
    foreign_key_dictionary: dict,
    source_aliases: set[str],
    target_aliases: set[str],
    prune: bool,
) -> tuple[dict[str, dict], list[dict]]:
    existing_catalogue = _read_optional(root / "catalogue.json", {"version": 1, "objects": []})
    current_ids = {entry["id"] for entry in catalogue.get("objects", [])}
    stale_entries = [
        entry
        for entry in existing_catalogue.get("objects", [])
        if _in_build_scope(entry, source_aliases, target_aliases)
        and entry.get("id") not in current_ids
    ]
    stale_ids = {entry["id"] for entry in stale_entries} if prune else set()

    merged_catalogue = {
        "version": 1,
        "objects": _merge_rows(
            existing_catalogue.get("objects", []),
            catalogue.get("objects", []),
            key="id",
            remove_ids=stale_ids,
        ),
    }

    merged_dependency = {
        "version": 1,
        "objects": _merge_mapping(
            _read_optional(root / "load_dependency.json", {"version": 1, "objects": {}}).get(
                "objects", {}
            ),
            load_dependency.get("objects", {}),
            remove_ids=stale_ids,
        ),
    }

    merged_table = {
        "version": 1,
        "tables": _merge_rows(
            _read_optional(root / "table_dictionary.json", {"version": 1, "tables": []}).get(
                "tables", []
            ),
            table_dictionary.get("tables", []),
            key="id",
            remove_ids=stale_ids,
        ),
    }
    merged_columns = {
        "version": 1,
        "columns": _merge_object_rows(
            _read_optional(root / "column_dictionary.json", {"version": 1, "columns": []}).get(
                "columns", []
            ),
            column_dictionary.get("columns", []),
            object_key="object_id",
            current_ids=current_ids,
            remove_ids=stale_ids,
        ),
    }
    merged_indexes = {
        "version": 1,
        "indexes": _merge_object_rows(
            _read_optional(root / "index_dictionary.json", {"version": 1, "indexes": []}).get(
                "indexes", []
            ),
            index_dictionary.get("indexes", []),
            object_key="object_id",
            current_ids=current_ids,
            remove_ids=stale_ids,
        ),
    }
    merged_foreign_keys = {
        "version": 1,
        "foreign_keys": _merge_object_rows(
            _read_optional(
                root / "foreign_key_dictionary.json",
                {"version": 1, "foreign_keys": []},
            ).get("foreign_keys", []),
            foreign_key_dictionary.get("foreign_keys", []),
            object_key="object_id",
            current_ids=current_ids,
            remove_ids=stale_ids,
        ),
    }

    return (
        {
            "catalogue": merged_catalogue,
            "load_dependency": merged_dependency,
            "table_dictionary": merged_table,
            "column_dictionary": merged_columns,
            "index_dictionary": merged_indexes,
            "foreign_key_dictionary": merged_foreign_keys,
        },
        stale_entries if prune else [],
    )


def _read_optional(path: Path, default: dict) -> dict:
    if not path.is_file():
        return default
    return read_json(path)


def _in_build_scope(entry: dict, source_aliases: set[str], target_aliases: set[str]) -> bool:
    return (
        entry.get("source_database") in source_aliases
        and entry.get("target_database") in target_aliases
    )


def _merge_rows(existing: list[dict], current: list[dict], *, key: str, remove_ids: set[str]) -> list[dict]:
    current_by_key = {row[key]: row for row in current}
    merged = [
        row
        for row in existing
        if row.get(key) not in current_by_key and row.get(key) not in remove_ids
    ]
    merged.extend(current)
    return merged


def _merge_mapping(existing: dict, current: dict, *, remove_ids: set[str]) -> dict:
    merged = {
        object_id: dependencies
        for object_id, dependencies in existing.items()
        if object_id not in current and object_id not in remove_ids
    }
    merged.update(current)
    return merged


def _merge_object_rows(
    existing: list[dict],
    current: list[dict],
    *,
    object_key: str,
    current_ids: set[str],
    remove_ids: set[str],
) -> list[dict]:
    merged = [
        row
        for row in existing
        if row.get(object_key) not in current_ids and row.get(object_key) not in remove_ids
    ]
    merged.extend(current)
    return merged


def _remove_pruned_sources(root: Path, catalogue: dict, pruned_entries: list[dict]) -> None:
    if not pruned_entries:
        return
    still_installed = {
        entry.get("installed_source")
        for entry in catalogue.get("objects", [])
        if entry.get("installed_source")
    }
    for entry in pruned_entries:
        installed_source = entry.get("installed_source")
        if not installed_source or installed_source in still_installed:
            continue
        source_path = root / installed_source
        if source_path.is_file():
            source_path.unlink()


def _apply_object_installs(host_objects, host_root: Path) -> tuple[str, ...]:
    created: list[str] = []
    for planned in host_objects:
        adapter = get_adapter(planned.target_type)
        action = adapter.apply(planned, host_root)
        if action.applied:
            created.append(planned.materialisation)
    return tuple(created)


def _install_sql_targets(plan, target_by_alias) -> list[SqlInstall]:
    from ..targets.sql import SQL_LOAD_PROCEDURE

    installs: list[SqlInstall] = []
    for alias, resolved in target_by_alias.items():
        if not resolved.is_sql:
            continue
        objects = [o for o in plan.objects if o.target_alias == alias]
        if not objects:
            continue
        adapter = get_adapter("SQL")
        actions = tuple(adapter.plan(o, None) for o in objects)
        installs.append(
            SqlInstall(
                target=alias,
                server=resolved.host,
                database=resolved.database,
                degrees_of_parallelism=resolved.degrees_of_parallelism,
                load_procedure=SQL_LOAD_PROCEDURE,
                actions=actions,
            )
        )
    return installs


def _apply_prune(plan, previous, target_by_alias) -> tuple[PruneItem, ...]:
    if not plan.prune or not previous:
        return ()
    items = plan_prune(plan, previous)
    for item in items:
        resolved = target_by_alias.get(item.target_alias)
        if resolved is not None and resolved.is_files:
            path = lakehouse_root(resolved) / item.materialisation
            shutil.rmtree(path, ignore_errors=True)
    return items
