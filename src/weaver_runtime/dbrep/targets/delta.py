"""Delta target adapter: plan Delta table installs for Table objects.

Plan-only at build time. Real Delta initialisation and load happen through the
Spark runtime (kept out of core imports so PySpark stays an optional dependency).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from ..config.databases import DELTA
from .base import InstallAction, TargetAdapter

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..build.planner import PlannedObject


class DeltaTarget(TargetAdapter):
    """Describes Delta table structure for Table objects."""

    type = DELTA
    plan_only = True

    def plan(self, planned: "PlannedObject", host_root: Path | None) -> InstallAction:
        self.validate_kind(planned.kind)
        metadata = planned.source.metadata
        operations = ["initialise delta table"]
        if metadata.schema:
            operations.append("apply schema declaration")
        if metadata.has_primary_key:
            operations.append("record primary key")
        operations.append(
            "record incremental policy"
            if metadata.is_incremental
            else "record complete-reconciliation policy"
        )
        if metadata.static:
            operations.append("record static flag")
        operations.append(f"record load mode: {metadata.effective_load_mode}")

        absolute = None if host_root is None else str(Path(host_root) / planned.materialisation)
        return InstallAction(
            id=planned.id,
            kind=planned.kind,
            target_type=self.type,
            materialisation=planned.materialisation,
            absolute_path=absolute,
            operations=tuple(operations),
            applied=False,
        )
