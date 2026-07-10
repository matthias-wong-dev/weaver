"""Shared Microsoft Fabric substrate: auth, REST client, resources, OneLake,
folder sync, Livy, and SQL. Environment-neutral; no product/operation defaults.
"""

from __future__ import annotations

from .context import resolve_lakehouse_target
from .onelake import LakehouseTarget
from .settings import (
    DEFAULT_DEGREES_OF_PARALLELISM,
    FabricSettings,
    resolve_settings,
)

__all__ = [
    "DEFAULT_DEGREES_OF_PARALLELISM",
    "FabricSettings",
    "LakehouseTarget",
    "resolve_lakehouse_target",
    "resolve_settings",
]
