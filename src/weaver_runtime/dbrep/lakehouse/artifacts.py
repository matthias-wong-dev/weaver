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
class RuntimeComponent:
    """One independently reconciled portion of an installed runtime."""

    kind: str
    name: str
    local_root: Path
    remote_root: str


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
    runtime_components: tuple[RuntimeComponent, ...]


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
        artifacts.append(
            generate_lakehouse_host_artifact(
                plan,
                group,
                out_dir / group.server,
                program=programs[group.server],
            )
        )
    return artifacts


def generate_lakehouse_host_artifact(
    plan,
    group: HostGroup,
    host_root: Path,
    *,
    program: str | None = None,
    initial_runtime_metadata: dict[str, dict] | None = None,
) -> LakehouseHostArtifact:
    """Stage exactly one already-grouped Lakehouse build.

    Fabric resolves physical host identity before calling this helper, so aliases
    pointing at the same Lakehouse are deliberately installed into one staging
    root and produce one program and one set of runtime components.
    """

    host_root = Path(host_root)
    runtime_root = host_root / "Files" / "_weaver" / "runtime"
    for name, document in (initial_runtime_metadata or {}).items():
        write_json(runtime_root / name, document)

    staged_pairs = tuple(
        BuildPair(
            pair.source,
            replace(
                pair.target,
                server_alias=group.server,
                host=str(host_root),
                server_type="Local Lakehouse",
                fabric_workspace=None,
                fabric_lakehouse=None,
            ),
        )
        for pair in group.pairs
    )
    install_build(replace(plan, pairs=staged_pairs))

    rendered = program if program is not None else render_host_program(group)
    build_program_path = host_root / BUILD_PROGRAM_NAME
    build_program_path.write_text(rendered, encoding="utf-8")
    plan_path = host_root / BUILD_PLAN_NAME
    write_json(plan_path, build_plan_document(group, program_name=BUILD_PROGRAM_NAME))

    databases = sorted({pair.source.database for pair in group.pairs})
    components = [
        RuntimeComponent(
            kind="builtin",
            name="weaver",
            local_root=runtime_root / "_orchestrator",
            remote_root="_weaver/runtime/_orchestrator",
        )
    ]
    components.extend(
        RuntimeComponent(
            kind="database",
            name=database,
            local_root=runtime_root / "objects" / database,
            remote_root=f"_weaver/runtime/objects/{database}",
        )
        for database in databases
    )
    return LakehouseHostArtifact(
        server=group.server,
        targets=group.target_aliases,
        root=host_root,
        files_root=host_root / "Files",
        build_program_path=build_program_path,
        plan_path=plan_path,
        object_ids=tuple(obj.id for obj in group.objects),
        program=rendered,
        runtime_components=tuple(components),
    )


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
