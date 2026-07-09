"""Install the runtime bundle and artifacts under a Lakehouse host.

For each Lakehouse host among the targets, this copies a self-contained
orchestrator, snapshots the supplied source databases (preserving layout,
including ``_helpers``), applies Files folder installs, and writes
``manifest.json`` / ``load_plan.json`` / ``source_hashes.json`` under
``Files/_weaver/runtime``. SQL targets are recorded as plan-only installs.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path

from ..config.resolution import lakehouse_root, runtime_root, ses_source_root
from ..targets import InstallAction, get_adapter
from .manifest import (
    RUNTIME_RELATIVE_ROOT,
    build_load_plan,
    build_manifest,
    build_source_hashes,
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


@dataclass
class HostInstall:
    """Result of installing one Lakehouse host's runtime bundle."""

    server: str
    runtime_root: str
    installed_objects: tuple[str, ...]
    files_created: tuple[str, ...]
    manifest_path: str
    load_plan_path: str
    source_hashes_path: str
    manifest: dict = field(default_factory=dict)
    load_plan: dict = field(default_factory=dict)


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
            )
        )
    return installs


def _install_one_host(representative, host_objects, source_by_alias, external, installed_at) -> HostInstall:
    root = runtime_root(representative)
    host_root = lakehouse_root(representative)
    root.mkdir(parents=True, exist_ok=True)

    _install_orchestrator(root)

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
    load_plan = build_load_plan(
        host_objects,
        server=representative.server_alias,
        targets={o.target_alias for o in host_objects},
    )
    hashes = build_source_hashes(host_objects)

    manifest_path = root / "manifest.json"
    load_plan_path = root / "load_plan.json"
    hashes_path = root / "source_hashes.json"
    write_json(manifest_path, manifest)
    write_json(load_plan_path, load_plan)
    write_json(hashes_path, hashes)

    return HostInstall(
        server=representative.server_alias,
        runtime_root=str(root),
        installed_objects=tuple(o.id for o in host_objects),
        files_created=files_created,
        manifest_path=str(manifest_path),
        load_plan_path=str(load_plan_path),
        source_hashes_path=str(hashes_path),
        manifest=manifest,
        load_plan=load_plan,
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


def _install_sources(root: Path, source_by_db: dict) -> None:
    for database, source in source_by_db.items():
        source_root = ses_source_root(source)
        destination = root / database
        if destination.exists():
            shutil.rmtree(destination)
        shutil.copytree(source_root, destination, ignore=_IGNORE)

        shared_helpers = source_root.parent / "_helpers"
        if shared_helpers.is_dir():
            shutil.copytree(
                shared_helpers,
                root / "_helpers",
                dirs_exist_ok=True,
                ignore=_IGNORE,
            )


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
