"""Base target-adapter interface and registry.

An adapter validates object kinds for its type, plans the physical install, and
optionally applies it. Filesystem targets (Files) apply for real; Delta and SQL
are plan-only at this stage and materialise during load / SQL install.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

from ..build.compatibility import validate_object_kind
from ..config.databases import DELTA, FILES, SQL
from ..errors import BuildError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..build.planner import PlannedObject


@dataclass(frozen=True)
class InstallAction:
    """A planned (or applied) install for one object."""

    id: str
    kind: str
    target_type: str
    materialisation: str
    absolute_path: str | None
    operations: tuple[str, ...]
    applied: bool

    def as_applied(self) -> "InstallAction":
        return replace(self, applied=True)


class TargetAdapter(ABC):
    """Adapter for one target representation type."""

    type: str
    plan_only: bool = True

    def validate_kind(self, kind: str) -> None:
        validate_object_kind(kind, self.type)

    @abstractmethod
    def plan(self, planned: "PlannedObject", host_root: Path | None) -> InstallAction:
        """Describe the install without touching anything."""

    def apply(self, planned: "PlannedObject", host_root: Path) -> InstallAction:
        """Apply the install. Plan-only adapters return an unapplied action."""

        return self.plan(planned, host_root)


_ADAPTERS: dict[str, TargetAdapter] | None = None


def get_adapter(target_type: str) -> TargetAdapter:
    global _ADAPTERS
    if _ADAPTERS is None:
        # Imported lazily to avoid import cycles at module load.
        from .delta import DeltaTarget
        from .files import FilesTarget
        from .sql import SqlTarget

        _ADAPTERS = {FILES: FilesTarget(), DELTA: DeltaTarget(), SQL: SqlTarget()}
    adapter = _ADAPTERS.get(target_type)
    if adapter is None:
        raise BuildError(f"no target adapter for type {target_type!r}")
    return adapter
