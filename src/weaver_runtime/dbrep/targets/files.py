"""Files target adapter: initialise Lakehouse folders for Folder objects."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from ..config.databases import FILES
from ..errors import BuildError
from .base import InstallAction, TargetAdapter

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..build.planner import PlannedObject

MARKER_NAME = "_weaver.json"


class FilesTarget(TargetAdapter):
    """Materialises Folder objects as managed folders under ``Files/``."""

    type = FILES
    plan_only = False

    def plan(self, planned: "PlannedObject", host_root: Path | None) -> InstallAction:
        self.validate_kind(planned.kind)
        absolute = None if host_root is None else str(Path(host_root) / planned.materialisation)
        return InstallAction(
            id=planned.id,
            kind=planned.kind,
            target_type=self.type,
            materialisation=planned.materialisation,
            absolute_path=absolute,
            operations=("create folder", "write managed marker"),
            applied=False,
        )

    def apply(self, planned: "PlannedObject", host_root: Path) -> InstallAction:
        if host_root is None:
            raise BuildError("Files install requires a host root")
        action = self.plan(planned, host_root)
        path = Path(host_root) / planned.materialisation
        path.mkdir(parents=True, exist_ok=True)
        marker = {
            "managed_by": "weaver",
            "id": planned.id,
            "kind": planned.kind,
            "materialisation": planned.materialisation,
        }
        (path / MARKER_NAME).write_text(json.dumps(marker, indent=2), encoding="utf-8")
        return replace(action, applied=True)
