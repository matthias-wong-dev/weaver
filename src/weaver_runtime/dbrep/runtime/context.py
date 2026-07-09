"""Execution context and dependency repository for load."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable


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
    """Per-object context passed to a Weaver object during load."""

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
