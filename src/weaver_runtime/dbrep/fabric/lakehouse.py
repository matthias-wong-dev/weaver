"""Fabric Lakehouse build and load.

Both operations follow the same shape as the local path: render one complete
Weaver program from the plan, then hand it to the *generic* Livy runtime
submitter (``weaver_runtime.fabric.livy.run_runtime_program``). This module holds
no operation-specific Spark/Python templates and no operation-specific result
markers — it only stages/uploads artifacts and resolves the Fabric target.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
import tempfile

from ..build.manifest import write_json
from ..lakehouse.artifacts import (
    COMPLETION_RECORD_NAME,
    completion_record,
    generate_lakehouse_artifacts,
    group_lakehouse_objects_by_host,
)
from ..lakehouse.programs import render_load_program
from . import onelake


@dataclass
class FabricBuildResult:
    targets: tuple[str, ...]
    workspace: str
    lakehouse: str
    uploaded: int
    runtime_root: str
    result: dict | None = None


def build_fabric_lakehouse(plan, fabric_pairs) -> list[FabricBuildResult]:
    """Build each Fabric Lakehouse host from one generated ``build.py``.

    Per physical host: stage the ``Files/`` artifact, upload it via OneLake, then
    submit the exact generated program once through the generic Livy runtime
    submitter. A completion record is written only after that program succeeds.
    """

    fabric_plan = replace(plan, pairs=tuple(fabric_pairs))
    results: list[FabricBuildResult] = []
    for group in group_lakehouse_objects_by_host(fabric_plan, fabric=True):
        representative = group.pairs[0].target
        with tempfile.TemporaryDirectory(prefix="weaver_fabric_stage_") as tmp:
            host_plan = replace(plan, pairs=group.pairs)
            (artifact,) = generate_lakehouse_artifacts(host_plan, Path(tmp))

            resolved = onelake.resolve_lakehouse(
                representative.fabric_workspace, representative.fabric_lakehouse
            )
            uploaded = onelake.sync_runtime_folder(artifact.files_root, resolved)

            result = _run_program(resolved, artifact.program)

            _write_completion_record(artifact, resolved, completion_record(group, result))

            results.append(
                FabricBuildResult(
                    targets=group.target_aliases,
                    workspace=resolved["workspace_name"],
                    lakehouse=resolved["lakehouse_name"],
                    uploaded=uploaded,
                    runtime_root="Files/_weaver/runtime",
                    result=result,
                )
            )
    return results


def load_fabric_lakehouse(
    target,
    *,
    object_filter: tuple[str, ...] | None = None,
    include_static: bool = False,
    strict: bool = True,
    poll_interval: float = 10.0,
    timeout: float = 1800.0,
) -> dict:
    """Run one generated load program in Fabric Spark via the generic submitter.

    Honours the same target/object/static/strict selection as the local load, so
    a Fabric load of one alias does not run every loadable target in the same
    physical Lakehouse.
    """

    resolved = onelake.resolve_lakehouse(target.fabric_workspace, target.fabric_lakehouse)
    program = render_load_program(
        target_filter=target.alias,
        object_filter=object_filter,
        include_static=include_static,
        strict=strict,
    )
    report = _run_program(resolved, program, poll_interval=poll_interval, timeout=timeout)
    return {
        "target": target.alias,
        "type": "Fabric Lakehouse",
        "workspace": resolved["workspace_name"],
        "lakehouse": resolved["lakehouse_name"],
        "executed": True,
        "report": report,
    }


def _run_program(
    resolved,
    program: str,
    *,
    poll_interval: float = 10.0,
    timeout: float = 1800.0,
) -> dict:
    """Submit a generated Weaver program through the generic Livy runtime."""

    from weaver_runtime.fabric import auth, livy
    from weaver_runtime.fabric.settings import resolve_settings

    settings = resolve_settings()
    return livy.run_runtime_program(
        resolved["workspace_id"],
        resolved["lakehouse_id"],
        auth.get_token(settings.fabric_scope),
        program,
        api_base_url=settings.api_base_url,
        api_version=settings.livy_api_version,
        poll_interval=poll_interval,
        timeout=timeout,
    )


def _write_completion_record(artifact, resolved, record: dict) -> None:
    """Write the completion record into the staged runtime and upload it."""

    record_path = artifact.files_root / "_weaver" / "runtime" / COMPLETION_RECORD_NAME
    write_json(record_path, record)
    onelake.sync_runtime_folder(artifact.files_root, resolved)
