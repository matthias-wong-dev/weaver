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
    :meth:`staging_folder`. ``workflow_id`` and ``log_dir`` identify durable
    workflow logging; ``staging_path`` is the exact object-local staging sibling.
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
    staging_path: Path | None = None
    _issued_staging: "StagingFolder | None" = field(default=None, repr=False)
    _staging_requested: bool = field(default=False, repr=False)

    def prepare_staging(self) -> "StagingFolder":
        """Reset and create this Folder step's deterministic staging path."""

        from .folders import new_staging_folder

        if self.staging_path is None or self.object_path is None:
            raise LoadError(f"no object-local staging path available for {self.object_id}")
        if self._issued_staging is None:
            self._issued_staging = new_staging_folder(
                self.staging_path,
                destination=self.object_path,
                lakehouse_root=self.lakehouse_root,
            )
        return self._issued_staging

    def staging_folder(self) -> "StagingFolder":
        """Return this step's empty object-local staging directory.

        Object code writes its retained output into ``staging.path`` and returns
        the ``(staging_folder, delete)`` pair; Weaver reconciles the
        staged files into the destination and calculates file CRUD.
        """

        staging = self.prepare_staging()
        if self._staging_requested:
            raise LoadError(f"Folder staging has already been created for {self.object_id}")
        self._staging_requested = True
        return staging

    def issued_staging(self) -> tuple["StagingFolder", ...]:
        """Staging folders issued to this object during the current step."""

        return () if self._issued_staging is None else (self._issued_staging,)

    def cleanup_staging(self) -> None:
        """Remove only this object's validated staging sibling after success."""

        staging = self._issued_staging
        if staging is None:
            return
        if self.staging_path is None or self.object_path is None:
            raise LoadError(f"cannot safely clean staging for {self.object_id}")

        from .folders import validate_staging_path_pair

        _lakehouse, _destination, expected = validate_staging_path_pair(
            self.lakehouse_root, self.object_path, self.staging_path
        )
        if staging.path.resolve() != expected:
            raise LoadError(f"issued staging path changed for {self.object_id}")
        try:
            shutil.rmtree(staging.path)
        except Exception as exc:
            raise LoadError(
                f"Folder {self.object_id} reconciled successfully but staging cleanup failed: {exc}"
            ) from exc
        self._issued_staging = None
