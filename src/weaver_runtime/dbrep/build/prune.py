"""Prune planning: remove previously Weaver-managed objects that are gone.

Prune only affects objects a prior build recorded as managed for a target that
the current build also covers. Objects outside the current build's targets, and
objects that were never Weaver-managed, are never pruned.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .planner import BuildPlan


@dataclass(frozen=True)
class PreviousObject:
    """A previously installed, Weaver-managed object (from a prior manifest)."""

    id: str
    kind: str
    materialisation: str
    target_alias: str


@dataclass(frozen=True)
class PruneItem:
    """An object to remove because it is no longer declared."""

    id: str
    kind: str
    materialisation: str
    target_alias: str


def plan_prune(plan: BuildPlan, previous: Iterable[PreviousObject]) -> tuple[PruneItem, ...]:
    """Compute objects to prune for the targets covered by ``plan``."""

    current_ids = {planned.id for planned in plan.objects}
    covered_targets = {planned.target_alias for planned in plan.objects}

    removed = [
        PruneItem(
            id=previous_object.id,
            kind=previous_object.kind,
            materialisation=previous_object.materialisation,
            target_alias=previous_object.target_alias,
        )
        for previous_object in previous
        if previous_object.target_alias in covered_targets
        and previous_object.id not in current_ids
    ]
    return tuple(sorted(removed, key=lambda item: item.id))
