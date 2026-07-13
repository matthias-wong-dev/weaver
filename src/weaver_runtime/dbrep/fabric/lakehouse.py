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
import json
import tempfile

from ..lakehouse.artifacts import (
    COMPLETION_RECORD_NAME,
    HostGroup,
    completion_record,
    generate_lakehouse_host_artifact,
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
    environment: str | None = None
    environment_id: str | None = None
    result: dict | None = None


def build_fabric_lakehouse(plan, fabric_pairs) -> list[FabricBuildResult]:
    """Build each Fabric Lakehouse host from one generated ``build.py``.

    Per physical host: stage the ``Files/`` artifact, upload it via OneLake, then
    submit the exact generated program once through the generic Livy runtime
    submitter. A completion record is written only after that program succeeds.
    """

    results: list[FabricBuildResult] = []
    for group, resolved in _group_by_physical_lakehouse(plan, fabric_pairs):
        environment_names = {
            pair.target.environment for pair in group.pairs if pair.target.environment
        }
        if len(environment_names) > 1:
            raise ValueError(
                "grouped Fabric Lakehouse targets require one effective Environment; got "
                + ", ".join(sorted(environment_names))
            )
        environment_name = next(iter(environment_names), None)
        with tempfile.TemporaryDirectory(prefix="weaver_fabric_stage_") as tmp:
            host_plan = replace(plan, pairs=group.pairs)
            existing_metadata = onelake.read_runtime_metadata(resolved)
            artifact = generate_lakehouse_host_artifact(
                host_plan,
                group,
                Path(tmp) / "lakehouse",
                initial_runtime_metadata=existing_metadata,
            )
            uploaded = onelake.sync_runtime_folder(
                artifact.files_root,
                resolved,
                runtime_components=artifact.runtime_components,
            )

            result, environment_id = _run_program(
                resolved, artifact.program, environment_name=environment_name
            )

            _publish_completion_record(resolved, completion_record(group, result))

            results.append(
                FabricBuildResult(
                    targets=group.target_aliases,
                    workspace=resolved["workspace_name"],
                    lakehouse=resolved["lakehouse_name"],
                    uploaded=uploaded,
                    runtime_root="Files/_weaver/runtime",
                    environment=environment_name,
                    environment_id=environment_id,
                    result=result,
                )
            )
    return results


def _group_by_physical_lakehouse(plan, fabric_pairs) -> list[tuple[HostGroup, dict]]:
    """Resolve first, then group aliases by immutable Fabric resource ids."""

    order_index = {object_id: index for index, object_id in enumerate(plan.order)}
    grouped: dict[tuple[str, str], tuple[dict, list]] = {}
    for pair in fabric_pairs:
        resolved = onelake.resolve_lakehouse(
            pair.target.fabric_workspace, pair.target.fabric_lakehouse
        )
        key = (resolved["workspace_id"], resolved["lakehouse_id"])
        if key not in grouped:
            grouped[key] = (resolved, [])
        grouped[key][1].append(pair)

    groups: list[tuple[HostGroup, dict]] = []
    for resolved, pairs in grouped.values():
        aliases = {pair.target.alias for pair in pairs}
        objects = tuple(
            sorted(
                (obj for obj in plan.objects if obj.target_alias in aliases),
                key=lambda obj: order_index[obj.id],
            )
        )
        groups.append(
            (
                HostGroup(
                    server=pairs[0].target.server_alias,
                    pairs=tuple(pairs),
                    objects=objects,
                ),
                resolved,
            )
        )
    return groups


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
    report, environment_id = _run_program(
        resolved, program, environment_name=target.environment,
        poll_interval=poll_interval, timeout=timeout
    )
    return {
        "target": target.alias,
        "type": "Fabric Lakehouse",
        "workspace": resolved["workspace_name"],
        "lakehouse": resolved["lakehouse_name"],
        "environment": target.environment,
        "environment_id": environment_id,
        "executed": True,
        "report": report,
    }


def _run_program(
    resolved,
    program: str,
    *,
    poll_interval: float = 10.0,
    timeout: float = 1800.0,
    environment_name: str | None = None,
) -> tuple[dict, str | None]:
    """Submit a generated Weaver program through the generic Livy runtime."""

    import os

    from weaver_runtime.fabric import auth, livy, resources
    from weaver_runtime.fabric.settings import resolve_settings

    settings = resolve_settings()
    # Optionally attach a Fabric Spark Environment (for pip libraries the default
    # runtime lacks) so the same generated program can run where deps are needed.
    token = auth.get_token(settings.fabric_scope)
    environment_id = (
        resources.resolve_environment_id(
            token, resolved["workspace_id"], environment_name, settings.api_base_url
        )
        if environment_name else os.environ.get("WEAVER_FABRIC_ENVIRONMENT_ID") or None
    )
    result = livy.run_runtime_program(
        resolved["workspace_id"],
        resolved["lakehouse_id"],
        token,
        program,
        api_base_url=settings.api_base_url,
        api_version=settings.livy_api_version,
        poll_interval=poll_interval,
        timeout=timeout,
        environment_id=environment_id,
    )
    return result, environment_id


def _publish_completion_record(resolved, record: dict) -> None:
    """Upload just the completion record after a successful build program.

    A single focused OneLake file write, not a second full ``Files/`` sync.
    """

    content = (json.dumps(record, indent=2) + "\n").encode("utf-8")
    onelake.upload_file(resolved, f"_weaver/runtime/{COMPLETION_RECORD_NAME}", content)
