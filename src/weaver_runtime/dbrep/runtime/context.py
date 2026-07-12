"""Execution context and dependency repository for load."""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from ..errors import LoadError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .folders import StagingFolder


class Repo:
    """Resolves object dependency ids to their loaded representations.

    The orchestrator registers a resolver that returns whatever a dependency
    produced (a folder path, a DataFrame, a table handle). Results are cached so
    a dependency referenced twice is resolved once.
    """

    def __init__(self, resolver: Callable[[str], Any] | None = None):
        self._resolver = resolver
        self._cache: dict[str, Any] = {}

    def register(self, resolver: Callable[[str], Any]) -> None:
        self._resolver = resolver

    def __getitem__(self, key: str) -> Any:
        if key not in self._cache:
            if self._resolver is None:
                raise KeyError(f"no resolver registered for dependency {key!r}")
            self._cache[key] = self._resolver(key)
        return self._cache[key]


@dataclass
class LoadContext:
    """Per-object context passed to a Weaver object during load.

    For Folder objects ``object_path`` is the destination directory (readable but
    not to be written directly); object code stages files through
    :meth:`staging_folder`. ``workflow_id``, ``log_dir`` and ``staging_root``
    carry the current workflow's durable-logging and staging locations.
    """

    runtime_root: Path
    lakehouse_root: Path
    object_id: str
    kind: str
    materialisation: str
    repo: Repo
    object_path: Path | None = None
    spark: Any = None
    metadata: Any = None
    extras: dict[str, Any] = field(default_factory=dict)
    workflow_id: str | None = None
    log_dir: Path | None = None
    staging_root: Path | None = None
    _issued_staging: list = field(default_factory=list, repr=False)

    def staging_folder(self) -> "StagingFolder":
        """Issue a fresh, empty staging directory beneath the workflow root.

        Object code writes its retained output into ``staging.path`` and returns
        the ``(staging_folder, delete)`` pair; Weaver reconciles the
        staged files into the destination and calculates file CRUD.
        """

        from .folders import new_staging_folder

        if self.staging_root is None:
            raise LoadError(
                f"no staging root available for {self.object_id}; Folder staging "
                "requires an active load workflow"
            )
        staging = new_staging_folder(self.staging_root)
        self._issued_staging.append(staging)
        return staging

    def issued_staging(self) -> tuple["StagingFolder", ...]:
        """Staging folders issued to this object during the current step."""

        return tuple(self._issued_staging)

    def cleanup_staging(self) -> None:
        """Remove every staging directory issued to this object.

        Only removes paths under ``staging_root`` so a validation failure that
        returned an arbitrary path can never delete outside the staging area.
        """

        root = self.staging_root
        for staging in self._issued_staging:
            if root is not None and _is_within(staging.path, root):
                shutil.rmtree(staging.path, ignore_errors=True)
        self._issued_staging.clear()


def _is_within(path: Path, root: Path) -> bool:
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
        return True
    except ValueError:
        return False
