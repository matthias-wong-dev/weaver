"""Turn a build request into an ordered, validated build plan.

A request is a set of ``from -> to`` database-representation pairs. Sources are
SES folders; targets are Files/Delta/SQL representations. The planner discovers
source objects, classifies dependencies against the supplied source databases,
validates the graph, and produces a topologically ordered plan plus the set of
external (unmanaged) dependencies.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config.resolution import (
    ResolvedDatabase,
    delta_materialisation,
    files_materialisation,
    ses_source_root,
)
from ..errors import BuildError
from ..ses.dependencies import Dependency, classify_object_dependencies
from .compatibility import validate_object_kind, validate_pair
from ..ses.discovery import SourceObject, discover_database
from ..ses.graph import topological_order

_EXTERNAL_REASON = "referenced but database representation was not supplied"


@dataclass(frozen=True)
class BuildPair:
    """One ``from -> to`` mapping between database representations."""

    source: ResolvedDatabase
    target: ResolvedDatabase


@dataclass(frozen=True)
class BuildRequest:
    """A build across one or more from/to pairs."""

    pairs: tuple[BuildPair, ...]
    prune: bool = False
    strict: bool = False


@dataclass(frozen=True)
class ExternalDependency:
    """A referenced object that is not managed by this build."""

    id: str
    reason: str


@dataclass(frozen=True)
class PlannedObject:
    """A source object bound to its target and managed dependencies."""

    source: SourceObject
    source_alias: str
    target_alias: str
    target_database: str
    target_type: str
    materialisation: str
    dependencies: tuple[Dependency, ...]

    @property
    def id(self) -> str:
        return self.source.id

    @property
    def declared_as(self) -> str:
        return self.source.declared_as

    @property
    def kind(self) -> str:
        return self.source.kind

    @property
    def source_database(self) -> str:
        return self.source.database


@dataclass(frozen=True)
class BuildPlan:
    """An ordered, validated build plan."""

    pairs: tuple[BuildPair, ...]
    objects: tuple[PlannedObject, ...]
    order: tuple[str, ...]
    external_dependencies: tuple[ExternalDependency, ...]
    prune: bool

    def object_by_id(self, object_id: str) -> PlannedObject:
        for planned in self.objects:
            if planned.id == object_id:
                return planned
        raise KeyError(object_id)


def discover_source_objects(source: ResolvedDatabase) -> tuple[SourceObject, ...]:
    """Discover object files for an SES source representation."""

    if not source.is_ses:
        raise BuildError(
            f"source {source.alias!r} must be an SES representation to build from"
        )
    return discover_database(ses_source_root(source), source.database)


def plan_build(request: BuildRequest) -> BuildPlan:
    """Produce a topologically ordered build plan for a request."""

    if not request.pairs:
        raise BuildError("build request must include at least one from/to pair")

    for pair in request.pairs:
        validate_pair(pair.source.type, pair.target.type)

    managed_databases = {pair.source.database for pair in request.pairs}

    discovered: dict[str, tuple[SourceObject, BuildPair]] = {}
    for pair in request.pairs:
        for source_object in discover_source_objects(pair.source):
            if source_object.id in discovered:
                raise BuildError(
                    f"object {source_object.id!r} is produced by more than one source"
                )
            discovered[source_object.id] = (source_object, pair)

    nodes = set(discovered)
    edges: list[tuple[str, str]] = []
    external: dict[str, str] = {}
    planned: list[PlannedObject] = []

    for object_id, (source_object, pair) in discovered.items():
        managed: list[Dependency] = []
        for dependency in classify_object_dependencies(source_object, managed_databases):
            if dependency.is_external:
                external.setdefault(dependency.id, _EXTERNAL_REASON)
                continue
            if dependency.id not in nodes:
                if dependency.is_intra:
                    raise BuildError(
                        f"missing intra-database dependency {dependency.id!r} "
                        f"required by {object_id!r}"
                    )
                raise BuildError(
                    f"missing managed cross-database dependency {dependency.id!r} "
                    f"required by {object_id!r}"
                )
            managed.append(dependency)
            edges.append((dependency.id, object_id))
        planned.append(_planned_object(source_object, pair, tuple(managed)))

    order = topological_order(nodes, edges)
    order_index = {object_id: index for index, object_id in enumerate(order)}
    planned.sort(key=lambda planned_object: order_index[planned_object.id])

    external_dependencies = tuple(
        ExternalDependency(id=object_id, reason=reason)
        for object_id, reason in sorted(external.items())
    )

    return BuildPlan(
        pairs=request.pairs,
        objects=tuple(planned),
        order=tuple(order),
        external_dependencies=external_dependencies,
        prune=request.prune,
    )


def _planned_object(
    source_object: SourceObject,
    pair: BuildPair,
    dependencies: tuple[Dependency, ...],
) -> PlannedObject:
    target = pair.target
    validate_object_kind(source_object.kind, target.type)
    schema = source_object.metadata.object_id.schema
    object_name = source_object.metadata.object_id.object
    return PlannedObject(
        source=source_object,
        source_alias=pair.source.alias,
        target_alias=target.alias,
        target_database=target.database,
        target_type=target.type,
        materialisation=_materialisation(target, schema, object_name),
        dependencies=dependencies,
    )


def _materialisation(target: ResolvedDatabase, schema: str, object_name: str) -> str:
    if target.is_files:
        return files_materialisation(target.database, schema, object_name)
    if target.is_delta:
        return delta_materialisation(
            target.database, schema, object_name, fabric=target.is_fabric
        )
    if target.is_sql:
        return f"{target.database}.{schema}.{object_name}"
    raise BuildError(
        f"target {target.alias!r} has unsupported type {target.type!r} for build"
    )


def format_dry_run(plan: BuildPlan) -> str:
    """Render a human-readable dry-run summary of a build plan."""

    lines: list[str] = []
    lines.append("build plan (dry run)")
    for pair in plan.pairs:
        lines.append(f"  {pair.source.alias} ({pair.source.type}) -> {pair.target.alias} ({pair.target.type})")
    lines.append("objects (load order):")
    for planned in plan.objects:
        scopes = ", ".join(f"{dep.id}[{dep.scope}]" for dep in planned.dependencies) or "none"
        lines.append(
            f"  {planned.id} :: {planned.kind} -> {planned.materialisation} (deps: {scopes})"
        )
    if plan.external_dependencies:
        lines.append("external/stable dependencies:")
        for external in plan.external_dependencies:
            lines.append(f"  {external.id} ({external.reason})")
    if plan.prune:
        lines.append("prune: enabled")
    return "\n".join(lines)
