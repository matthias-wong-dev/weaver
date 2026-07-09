"""Command logic for build/load/plan/discover/manifest.

Each ``run_*`` returns a plain dict so it is easy to test and to serialise. The
argparse layer in :mod:`.parser` prints the result as JSON.
"""

from __future__ import annotations

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

    previous = _read_previous_objects(plan) if prune else None
    result = install_build(plan, previous=previous)
    return {
        "dry_run": False,
        "built": list(plan.order),
        "hosts": [_host_summary(host) for host in result.hosts],
        "sql": [
            {
                "target": install.target,
                "server": install.server,
                "database": install.database,
                "degrees_of_parallelism": install.degrees_of_parallelism,
                "load_procedure": install.load_procedure,
                "objects": [action.id for action in install.actions],
            }
            for install in result.sql
        ],
        "pruned": [item.id for item in result.pruned],
        "external": [external.id for external in plan.external_dependencies],
    }


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
        from ..targets.sql import SQL_LOAD_PROCEDURE

        if dry_run:
            return {
                "target": target,
                "type": "SQL",
                "server": resolved.host,
                "database": resolved.database,
                "degrees_of_parallelism": resolved.degrees_of_parallelism,
                "load_procedure": SQL_LOAD_PROCEDURE,
                "action": "execute installed load stored procedure",
                "executed": False,
            }
        raise LoadError("SQL load execution is not implemented in this stage")

    raise LoadError(f"target {target!r} of type {resolved.type!r} cannot be loaded")


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
        manifest_path = root / "manifest.json"
        if not manifest_path.is_file():
            continue
        manifest = read_json(manifest_path)
        for entry in manifest.get("objects", []):
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
