"""Command logic for build/load/plan/discover/manifest.

Each ``run_*`` returns a plain dict so it is easy to test and to serialise. The
argparse layer in :mod:`.parser` prints the result as JSON.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from ..build import BuildPair, BuildRequest, format_dry_run, plan_build
from ..build.manifest import read_json
from ..build.prune import PreviousObject
from ..build.runtime_bundle import install_build
from ..config import load_databases_config, resolve_database
from ..config.resolution import runtime_root
from ..errors import BuildError, LoadError
from ..runtime.orchestrator import load_target_runtime


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
            "environments": {
                pair.target.alias: pair.target.environment
                for pair in plan.pairs
                if pair.target.is_fabric
            },
            "external": [external.id for external in plan.external_dependencies],
        }

    fabric_pairs = [p for p in plan.pairs if p.target.is_lakehouse and p.target.is_fabric]
    local_plan = replace(plan, pairs=tuple(p for p in plan.pairs if p not in fabric_pairs))

    # Render (and validate) each local host's build program before any side
    # effect: a missing Delta schema fails here, before install_build or Spark.
    host_programs = _render_local_build_programs(local_plan)

    previous = _read_previous_objects(local_plan) if prune else None
    result = install_build(local_plan, previous=previous)

    lakehouse = _execute_local_build_programs(host_programs)
    return {
        "dry_run": False,
        "built": list(plan.order),
        "hosts": [_host_summary(host) for host in result.hosts],
        "lakehouse": lakehouse,
        "sql": _build_sql_targets(plan),
        "fabric": _build_fabric_targets(plan, fabric_pairs),
        "pruned": [item.id for item in result.pruned],
        "external": [external.id for external in plan.external_dependencies],
    }


def _render_local_build_programs(plan) -> list[tuple]:
    """Render and validate the build program for each local Lakehouse host.

    Returns ``(group, lakehouse_root, runtime_root, program)`` tuples. Rendering
    validates the declared Delta schemas, so this raises before any side effect.
    """

    from ..config.resolution import lakehouse_root
    from ..config.resolution import runtime_root as _runtime_root
    from ..lakehouse.artifacts import group_lakehouse_objects_by_host, render_host_program

    programs: list[tuple] = []
    for group in group_lakehouse_objects_by_host(plan, fabric=False):
        program = render_host_program(group)
        representative = group.pairs[0].target
        programs.append(
            (group, lakehouse_root(representative), _runtime_root(representative), program)
        )
    return programs


def _execute_local_build_programs(host_programs) -> list[dict]:
    """Execute each rendered host build program against its local Lakehouse root.

    Runs the exact generated program through the generic local executor. When a
    host has Delta work, a working Spark/Delta session is required — build fails
    rather than silently skipping. A completion record is written only after the
    program succeeds. Local Lakehouse support is a test substrate, so a passing
    local build proves the generated Spark program actually executed.
    """

    if not host_programs:
        return []

    from ..build.manifest import write_json
    from ..execution import execute_program_local
    from ..lakehouse.artifacts import COMPLETION_RECORD_NAME, completion_record

    needs_spark = any(
        any(pair.target.is_delta for pair in group.pairs)
        for group, _root, _runtime_root, _program in host_programs
    )
    spark, own_spark = (None, False)
    if needs_spark:
        spark, own_spark = _acquire_delta_session(host_programs[0][1])
        if spark is None:
            raise BuildError(
                "local Lakehouse Delta build requires a working Spark/Delta "
                "session, but none could be created"
            )

    results: list[dict] = []
    try:
        for group, root, host_runtime_root, program in host_programs:
            result = execute_program_local(
                program, spark=spark, runtime_root=host_runtime_root, spark_root=root
            )
            write_json(
                host_runtime_root / COMPLETION_RECORD_NAME, completion_record(group, result)
            )
            results.append(
                {
                    "server": group.server,
                    "targets": list(group.target_aliases),
                    "result": result,
                }
            )
        return results
    finally:
        if own_spark and spark is not None:
            spark.stop()


def _acquire_delta_session(lakehouse_root=None):
    """Return ``(session, own)`` for local program execution.

    Reuses an already-active Spark session when present (``own`` False, so the
    caller must not stop it); otherwise creates a local Delta session rooted at
    the Lakehouse (``own`` True). Returns ``(None, False)`` when PySpark or a Java
    runtime is unavailable.
    """

    try:
        from pyspark.sql import SparkSession

        from ..runtime.load import create_delta_session
    except Exception:
        return None, False

    active = SparkSession.getActiveSession()
    if active is not None:
        return active, False
    try:
        return create_delta_session(lakehouse_root=lakehouse_root), True
    except Exception:
        return None, False


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
            "environment": result.environment,
            "environment_id": result.environment_id,
            "result": result.result,
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


def run_generate(
    config_path,
    from_arg: str,
    to_arg: str,
    *,
    out=None,
    prune: bool = False,
    strict: bool = False,
) -> dict:
    """Generate concrete deployment/runtime artifacts without applying them.

    SQL targets emit executable DDL scripts; Lakehouse targets stage the runtime
    bundle to ``out``; Fabric targets are staged locally and never uploaded.
    """

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

    out_dir = Path(out) if out else (Path(config_path).resolve().parent / ".weaver" / "generate")
    out_dir.mkdir(parents=True, exist_ok=True)

    lakehouse = _generate_lakehouse_artifacts(plan, out_dir)
    sql = _generate_sql_scripts(plan, out_dir)

    return {
        "generated": True,
        "out": str(out_dir),
        "objects": list(plan.order),
        "lakehouse": lakehouse,
        "sql": sql,
        "external": [external.id for external in plan.external_dependencies],
    }


def _generate_lakehouse_artifacts(plan, out_dir: Path) -> list[dict]:
    """Render a complete build artifact (Files + build.py + plan) per host.

    Uses the same host-artifact generator that local and Fabric build apply, so
    the generated program is the exact string both execution paths run.
    """

    from ..lakehouse.artifacts import generate_lakehouse_artifacts

    return [
        {
            "server": artifact.server,
            "targets": list(artifact.targets),
            "objects": list(artifact.object_ids),
            "root": str(artifact.root),
            "files_root": str(artifact.files_root),
            "build_program": str(artifact.build_program_path),
            "plan": str(artifact.plan_path),
        }
        for artifact in generate_lakehouse_artifacts(plan, out_dir)
    ]


def _generate_sql_scripts(plan, out_dir: Path) -> list[dict]:
    """Emit SQL deployment artifacts for each SQL target under ``out_dir``.

    Writes each source object's SQL and a ``plan.json`` describing the ordered
    install operations. Real backing-table shape is inferred at build time
    against the endpoint, so nothing here executes or requires a connection.
    """

    from ..build.manifest import write_json
    from ..targets import get_adapter

    results: list[dict] = []
    for pair in plan.pairs:
        if not pair.target.is_sql:
            continue
        adapter = get_adapter("SQL")
        objects = [obj for obj in plan.objects if obj.target_alias == pair.target.alias]
        if not objects:
            continue
        ordered = [obj for obj_id in plan.order for obj in objects if obj.id == obj_id]

        target_dir = out_dir / pair.target.alias
        objects_dir = target_dir / "objects"
        objects_dir.mkdir(parents=True, exist_ok=True)

        scripts: list[str] = []
        plan_entries: list[dict] = []
        for obj in ordered:
            action = adapter.plan(obj, None)
            script_path = objects_dir / f"{obj.materialisation}.sql"
            script_path.write_text(obj.source.text.rstrip() + "\n", encoding="utf-8")
            scripts.append(str(script_path))
            plan_entries.append(
                {
                    "id": obj.id,
                    "kind": obj.kind,
                    "materialisation": obj.materialisation,
                    "operations": list(action.operations),
                    "source": str(script_path),
                }
            )

        plan_path = target_dir / "plan.json"
        write_json(
            plan_path,
            {
                "version": 1,
                "target": pair.target.alias,
                "server": pair.target.host,
                "database": pair.target.database,
                "objects": plan_entries,
            },
        )
        results.append(
            {
                "target": pair.target.alias,
                "server": pair.target.host,
                "database": pair.target.database,
                "objects": len(plan_entries),
                "plan": str(plan_path),
                "scripts": scripts,
            }
        )
    return results


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
        object_filter = tuple(objects) if objects else None
        if resolved.is_fabric:
            if dry_run:
                return {
                    "target": target,
                    "type": "Fabric Lakehouse",
                    "workspace": resolved.fabric_workspace,
                    "lakehouse": resolved.fabric_lakehouse,
                    "environment": resolved.environment,
                    "executed": False,
                }
            from ..fabric.lakehouse import load_fabric_lakehouse

            return load_fabric_lakehouse(
                resolved,
                object_filter=object_filter,
                include_static=include_static,
                strict=strict,
            )

        root = runtime_root(resolved)
        payload = {"target": target, "runtime_root": str(root)}
        if dry_run:
            # Planning only: no program, no Spark — just validate + order steps.
            report = load_target_runtime(
                root,
                execute=False,
                object_filter=object_filter,
                target_filter=target,
                include_static=include_static,
                strict=strict,
            )
            payload.update(report.to_dict())
            return payload

        # Plan the selection first (no Spark). A load that includes Table steps
        # requires a working Spark/Delta session; fail clearly if none can be got.
        planned = load_target_runtime(
            root,
            execute=False,
            object_filter=object_filter,
            target_filter=target,
            include_static=include_static,
            strict=strict,
        )
        needs_spark = any(step.kind == "Table" for step in planned.steps)

        from ..config.resolution import lakehouse_root
        from ..execution import execute_program_local
        from ..lakehouse.programs import render_load_program

        spark, own_spark = (None, False)
        if needs_spark:
            spark, own_spark = _acquire_delta_session(lakehouse_root(resolved))
            if spark is None:
                raise LoadError(
                    "local Lakehouse load of table object(s) requires a working "
                    "Spark/Delta session, but none could be created"
                )

        # Execute the same generated load program the Fabric path submits.
        program = render_load_program(
            target_filter=target,
            object_filter=object_filter,
            include_static=include_static,
            strict=strict,
        )
        try:
            result = execute_program_local(
                program, spark=spark, runtime_root=root, spark_root=lakehouse_root(resolved)
            )
        finally:
            if own_spark and spark is not None:
                spark.stop()
        payload.update(result)
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

    Files -> ``Files/<database>``. Delta local -> ``Tables/<database>`` (the
    database's table dir among co-located databases); Delta Fabric -> ``Tables``
    (the Lakehouse *is* the database host, so its whole table area). Local hosts
    use a filesystem delete; Fabric hosts use a single OneLake recursive delete.
    """

    if resolved.is_files:
        relative = f"Files/{resolved.database}"
    elif resolved.is_fabric:
        relative = "Tables"
    else:
        relative = f"Tables/{resolved.database}"

    if resolved.is_fabric:
        from ..fabric import onelake

        info = onelake.resolve_lakehouse(resolved.fabric_workspace, resolved.fabric_lakehouse)
        existed = onelake.delete_directory(info, relative)
        location = f"{resolved.fabric_workspace}/{resolved.fabric_lakehouse}/{relative}"
    else:
        import shutil

        from ..config.resolution import lakehouse_root

        path = lakehouse_root(resolved) / relative
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
