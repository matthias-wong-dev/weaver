"""Target adapters that install physical structure for planned objects."""

from __future__ import annotations

from .base import InstallAction, TargetAdapter, get_adapter
from .delta import DeltaTarget
from .files import FilesTarget
from .sql import SqlTarget

__all__ = [
    "DeltaTarget",
    "FilesTarget",
    "InstallAction",
    "SqlTarget",
    "TargetAdapter",
    "get_adapter",
]
