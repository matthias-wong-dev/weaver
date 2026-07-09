"""Command logic for build/load/plan/discover/manifest.

Each ``run_*`` returns a plain dict so it is easy to test and to serialise. The
argparse layer in :mod:`.parser` prints the result as JSON.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from ..build import BuildPair, BuildRequest, format_dry_run, plan_build
from ..build.manifest import read_json
from ..build.planner import discover_source_objects
from ..build.prune import PreviousObject
from ..build.runtime_bundle import install_build
from ..config import load_databases_config, resolve_database
from ..config.resolution import runtime_root
from ..errors import BuildError, LoadError
from ..runtime.orchestrator import load_target_runtime
from ..ses.dependencies import classify_object_dependencies


def _load_config(config_path):
    return load_databases_config(config_path)


def _resolve(config, alias):
    return resolve_database(config.get(alias), config.environment)


def _split(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def run_build(
    config_path,
    from_arg: str,
    to_arg: str,
    *,
    prune: bool = False,
    dry_run: bool = False,
    strict: bool = False,
    assume_installed_runtime: bool = False,
) -> dict:
    config = _load_config(config_path)
    from_aliases = _split(from_arg)
    to_aliases = _split(to_arg)
    if not from_aliases or not to_aliases:
        raise BuildError("--from and --to are required")
    if len(from_aliases) != len(to_aliases):
        raise BuildError(
            f"--from has {len(from_aliases)} aliases but --to has {len(to_aliases)}"
        )

    pairs = tuple(
        BuildPair(_resolve(config, source), _resolve(config, target))
        for source, target in zip(from_aliases, to_aliases)
    )
    plan = plan_build(BuildRequest(pairs=pairs, prune=prune, strict=strict))

    if dry_run:
        return {
            "dry_run": True,
            "plan": format_dry_run(plan),
            "objects": list(plan.order),
            "external": [external.id for external in plan.external_dependencies],
        }

    fabric_pairs = [p for p in plan.pairs if p.target.is_lakehouse and p.target.is_fabric]
    local_plan = replace(plan, pairs=tuple(p for p in plan.pairs if p not in fabric_pairs))

    previous = _read_previous_objects(local_plan) if prune else None
    result = install_build(local_plan, previous=previous)
    return {
        "dry_run": False,
        "built": list(plan.order),
        "hosts": [_host_summary(host) for host in result.hosts],
        "sql": _build_sql_targets(plan),
        "fabric": _build_fabric_targets(plan, fabric_pairs),
        "pruned": [item.id for item in result.pruned],
        "external": [external.id for external in plan.external_dependencies],
    }


def _build_fabric_targets(plan, fabric_pairs) -> list[dict]:
    """Stage and upload any Fabric Lakehouse targets in the plan."""

    if not fabric_pairs:
        return []

    from ..fabric.lakehouse import build_fabric_lakehouse

    return [
        {
            "targets": list(result.targets),
            "workspace": result.workspace,
            "lakehouse": result.lakehouse,
            "uploaded": result.uploaded,
            "runtime_root": result.runtime_root,
        }
        for result in build_fabric_lakehouse(plan, fabric_pairs)
    ]


def _build_sql_targets(plan) -> list[dict]:
    """Execute real SQL builds for any SQL targets in the plan."""

    sql_pairs = [
        (pair.target, [o for o in plan.objects if o.target_alias == pair.target.alias])
        for pair in plan.pairs
        if pair.target.is_sql
    ]
    if not any(objects for _, objects in sql_pairs):
        return []

    from ..sql.backend import build_sql_target

    results: list[dict] = []
    for target, objects in sql_pairs:
        if not objects:
            continue
        built = build_sql_target(objects, target, prune=plan.prune)
        results.append(
            {
                "target": built.target,
                "server": built.server,
                "database": built.database,
                "schemas": list(built.schemas),
                "tables": list(built.tables),
                "views": list(built.views),
                "procedures": list(built.procedures),
                "pruned": list(built.pruned),
                "layers": [list(layer) for layer in built.layers],
            }
        )
    return results


def run_plan(config_path, from_arg: str, to_arg: str) -> dict:
    return run_build(config_path, from_arg, to_arg, dry_run=True)


def run_load(
    config_path,
    target: str,
    *,
    objects: tuple[str, ...] | None = None,
    include_static: bool = False,
    dry_run: bool = False,
    strict: bool = True,
) -> dict:
    config = _load_config(config_path)
    resolved = _resolve(config, target)

    if resolved.is_lakehouse:
        if resolved.is_fabric:
            if dry_run:
                return {
                    "target": target,
                    "type": "Fabric Lakehouse",
                    "workspace": resolved.fabric_workspace,
                    "lakehouse": resolved.fabric_lakehouse,
                    "executed": False,
                }
            from ..fabric.lakehouse import load_fabric_lakehouse

            return load_fabric_lakehouse(resolved)

        root = runtime_root(resolved)
        report = load_target_runtime(
            root,
            execute=not dry_run,
            object_filter=tuple(objects) if objects else None,
            target_filter=target,
            include_static=include_static,
            strict=strict,
        )
        payload = {"target": target, "runtime_root": str(root)}
        payload.update(report.to_dict())
        return payload

    if resolved.is_sql:
        if dry_run:
            from ..targets.sql import SQL_LOAD_PROCEDURE

            return {
                "target": target,
                "type": "SQL",
                "server": resolved.host,
                "database": resolved.database,
                "degrees_of_parallelism": resolved.degrees_of_parallelism,
                "load_procedure": SQL_LOAD_PROCEDURE,
                "action": "execute installed load stored procedures",
                "executed": False,
            }

        from ..sql.backend import load_sql_target

        result = load_sql_target(resolved, object_filter=objects)
        return {
            "target": target,
            "type": "SQL",
            "server": result.server,
            "database": result.database,
            "executed": True,
            "executed_procedures": list(result.executed),
        }

    raise LoadError(f"target {target!r} of type {resolved.type!r} cannot be loaded")


def run_wipe(config_path, target: str) -> dict:
    config = _load_config(config_path)
    resolved = _resolve(config, target)

    if resolved.is_sql:
        from ..sql.backend import wipe_sql_target

        result = wipe_sql_target(resolved)
        return {
            "target": target,
            "type": "SQL",
            "server": result.server,
            "database": result.database,
            "before": result.before,
            "after": result.after,
        }

    if resolved.is_lakehouse:
        return _wipe_lakehouse(target, resolved)

    raise LoadError(f"wipe does not support target type {resolved.type!r}")


def _wipe_lakehouse(target: str, resolved) -> dict:
    """Wipe a Files or Delta representation: its materialisations under the host.

    Files -> ``Files/<database>``; Delta -> ``Tables/<database>``. Local hosts use
    a filesystem delete; Fabric hosts use a single OneLake recursive delete.
    """

    subfolder = "Files" if resolved.is_files else "Tables"
    relative = f"{subfolder}/{resolved.database}"

    if resolved.is_fabric:
        from ..fabric import onelake

        info = onelake.resolve_lakehouse(resolved.fabric_workspace, resolved.fabric_lakehouse)
        existed = onelake.delete_directory(info, relative)
        location = f"{resolved.fabric_workspace}/{resolved.fabric_lakehouse}/{relative}"
    else:
        import shutil

        from ..config.resolution import lakehouse_root

        path = lakehouse_root(resolved) / subfolder / resolved.database
        existed = path.exists()
        shutil.rmtree(path, ignore_errors=True)
        location = str(path)

    return {
        "target": target,
        "type": resolved.type,
        "platform": "fabric" if resolved.is_fabric else "local",
        "wiped": relative,
        "location": location,
        "existed": existed,
    }


def run_discover(config_path, database: str) -> dict:
    config = _load_config(config_path)
    resolved = _resolve(config, database)
    objects = discover_source_objects(resolved)
    managed = {resolved.database}
    return {
        "database": database,
        "objects": [
            {
                "id": source_object.id,
                "kind": source_object.kind,
                "declared_as": source_object.declared_as,
                "language": source_object.language,
                "dependencies": [
                    {"id": dependency.id, "scope": dependency.scope}
                    for dependency in classify_object_dependencies(source_object, managed)
                ],
            }
            for source_object in objects
        ],
    }


def run_manifest(config_path, target: str) -> dict:
    config = _load_config(config_path)
    resolved = _resolve(config, target)
    if not resolved.is_lakehouse:
        raise LoadError(
            f"manifest is only available for Lakehouse targets, not {resolved.type}"
        )
    manifest_path = runtime_root(resolved) / "manifest.json"
    if not manifest_path.is_file():
        raise LoadError(f"no installed manifest for target {target!r} at {manifest_path}")
    return read_json(manifest_path)


def _read_previous_objects(plan) -> list[PreviousObject]:
    previous: list[PreviousObject] = []
    seen: set[str] = set()
    for pair in plan.pairs:
        target = pair.target
        if not target.is_lakehouse:
            continue
        root = runtime_root(target)
        if str(root) in seen:
            continue
        seen.add(str(root))
        catalogue_path = root / "catalogue.json"
        if not catalogue_path.is_file():
            continue
        catalogue = read_json(catalogue_path)
        for entry in catalogue.get("objects", []):
            previous.append(
                PreviousObject(
                    id=entry["id"],
                    kind=entry["kind"],
                    materialisation=entry["materialisation"],
                    target_alias=entry["target_database"],
                )
            )
    return previous


def _host_summary(host) -> dict:
    return {
        "server": host.server,
        "runtime_root": host.runtime_root,
        "objects": list(host.installed_objects),
        "files_created": list(host.files_created),
    }
