"""Generate complete Lakehouse host build artifacts from a build plan.

This is the single place that groups Lakehouse targets by physical host, orders
a host's objects, and renders its build program. ``run_generate`` writes these
artifacts without applying them; ``run_build`` (local and Fabric) generates them
into a temporary directory and applies them. The Delta spec derivation, schema
validation and program composition all live in :mod:`.programs`.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

from ..build.planner import BuildPair
from ..build.manifest import write_json
from ..build.runtime_bundle import install_build
from ..ses.metadata import FOLDER, TABLE
from ..config.databases import DELTA
from .programs import render_build_program

BUILD_PROGRAM_NAME = "build.py"
BUILD_PLAN_NAME = "build-plan.json"
COMPLETION_RECORD_NAME = "build_complete.json"


@dataclass(frozen=True)
class HostGroup:
    """One physical Lakehouse host's targets and ordered objects."""

    server: str
    pairs: tuple[BuildPair, ...]
    objects: tuple  # ordered PlannedObject tuple

    @property
    def target_aliases(self) -> tuple[str, ...]:
        return tuple(sorted(pair.target.alias for pair in self.pairs))


@dataclass(frozen=True)
class LakehouseHostArtifact:
    """A generated, not-yet-applied build artifact for one Lakehouse host."""

    server: str
    targets: tuple[str, ...]
    root: Path
    files_root: Path
    build_program_path: Path
    plan_path: Path
    object_ids: tuple[str, ...]
    program: str


def group_lakehouse_objects_by_host(plan, *, fabric: bool | None = None) -> list[HostGroup]:
    """Group a plan's Lakehouse targets by physical host in deterministic order.

    ``fabric`` filters to Fabric hosts (True), local hosts (False), or all
    (None). Objects within a host are ordered by the plan's topological order.
    """

    order_index = {object_id: index for index, object_id in enumerate(plan.order)}
    by_server: dict[str, list[BuildPair]] = {}
    for pair in plan.pairs:
        if not pair.target.is_lakehouse:
            continue
        if fabric is True and not pair.target.is_fabric:
            continue
        if fabric is False and pair.target.is_fabric:
            continue
        by_server.setdefault(pair.target.server_alias, []).append(pair)

    groups: list[HostGroup] = []
    for server, pairs in by_server.items():
        aliases = {pair.target.alias for pair in pairs}
        objects = tuple(
            sorted(
                (obj for obj in plan.objects if obj.target_alias in aliases),
                key=lambda obj: order_index[obj.id],
            )
        )
        groups.append(HostGroup(server=server, pairs=tuple(pairs), objects=objects))
    return groups


def render_host_program(group: HostGroup) -> str:
    """Render (and validate) the build program for one host group."""

    return render_build_program(group.objects)


def generate_lakehouse_artifacts(plan, out_dir) -> list[LakehouseHostArtifact]:
    """Generate one complete build artifact per physical Lakehouse host.

    Renders and validates every host program first (so a missing Delta schema
    fails before any artifact is written), then stages ``Files/`` via the
    existing ``install_build`` and writes ``build.py`` and ``build-plan.json``.
    """

    out_dir = Path(out_dir)
    groups = group_lakehouse_objects_by_host(plan)
    if not groups:
        return []

    # Validate + render every program up front — no writes on a schema failure.
    programs = {group.server: render_host_program(group) for group in groups}

    artifacts: list[LakehouseHostArtifact] = []
    for group in groups:
        host_root = out_dir / group.server
        staged_pairs = tuple(
            BuildPair(pair.source, replace(pair.target, host=str(host_root), platform="local"))
            for pair in group.pairs
        )
        install_build(replace(plan, pairs=staged_pairs))

        program = programs[group.server]
        build_program_path = host_root / BUILD_PROGRAM_NAME
        build_program_path.write_text(program, encoding="utf-8")

        plan_path = host_root / BUILD_PLAN_NAME
        write_json(plan_path, build_plan_document(group, program_name=BUILD_PROGRAM_NAME))

        artifacts.append(
            LakehouseHostArtifact(
                server=group.server,
                targets=group.target_aliases,
                root=host_root,
                files_root=host_root / "Files",
                build_program_path=build_program_path,
                plan_path=plan_path,
                object_ids=tuple(obj.id for obj in group.objects),
                program=program,
            )
        )
    return artifacts


def build_plan_document(group: HostGroup, *, program_name: str = BUILD_PROGRAM_NAME) -> dict:
    """A deterministic record of what a host build program will materialise."""

    return {
        "version": 1,
        "server": group.server,
        "targets": list(group.target_aliases),
        "objects": [obj.id for obj in group.objects],
        "build_program": program_name,
        "folders": [obj.materialisation for obj in group.objects if obj.kind == FOLDER],
        "delta_tables": [
            obj.materialisation
            for obj in group.objects
            if obj.target_type == DELTA and obj.kind == TABLE
        ],
    }


def completion_record(group: HostGroup, result: dict) -> dict:
    """The completion record written only after a build program succeeds."""

    from ..build.manifest import utc_now_iso

    return {
        "version": 1,
        "server": group.server,
        "targets": list(group.target_aliases),
        "objects": [obj.id for obj in group.objects],
        "result": result,
        "completed_at": utc_now_iso(),
    }
