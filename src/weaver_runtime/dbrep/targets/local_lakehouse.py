"""Local Lakehouse host layout.

A local Lakehouse is a filesystem host that co-locates ``Files/`` (folders),
``Tables/`` (Delta), and the Weaver runtime bundle under ``Files/_weaver``. It is
the same shape a Fabric Lakehouse presents through OneLake, so the manifest,
discovery convention, and load command are identical.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..config.resolution import (
    ResolvedDatabase,
    lakehouse_root,
    runtime_root,
    tables_root,
)
from ..config.resolution import files_root as _files_root


@dataclass(frozen=True)
class LocalLakehouseHost:
    """Filesystem view of a Lakehouse host shared by co-located representations."""

    host: str
    root: Path

    @classmethod
    def for_target(cls, target: ResolvedDatabase) -> "LocalLakehouseHost":
        return cls(host=target.host, root=lakehouse_root(target))

    def files_root(self, target: ResolvedDatabase) -> Path:
        return _files_root(target)

    def tables_root(self, target: ResolvedDatabase) -> Path:
        return tables_root(target)

    def runtime_root(self, target: ResolvedDatabase) -> Path:
        return runtime_root(target)
