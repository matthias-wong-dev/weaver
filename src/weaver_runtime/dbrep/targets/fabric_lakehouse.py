"""Fabric Lakehouse target interface (not implemented in this stage).

Fabric support is intentionally deferred. The interface is documented here so it
can be added later without changing the config model, database-representation
model, runtime discovery convention, manifest format, or load command shape: a
Fabric Lakehouse host maps ``Files/``, ``Tables/``, and ``Files/_weaver/runtime``
onto OneLake using the same relative materialisations the local host uses.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config.resolution import ResolvedDatabase
from ..errors import BuildError


@dataclass(frozen=True)
class FabricLakehouseHost:
    """Placeholder for a OneLake-backed Lakehouse host."""

    host: str

    @classmethod
    def for_target(cls, target: ResolvedDatabase) -> "FabricLakehouseHost":
        return cls(host=target.host)

    def install(self, *args, **kwargs):  # pragma: no cover - interface stub
        raise BuildError(
            "Fabric Lakehouse install is not implemented in this stage; "
            "the local Lakehouse host implements the same manifest/runtime shape"
        )
