"""Reject artifact writing (pure filesystem, no PySpark)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Sequence


def rejects_root(lakehouse_root: Path) -> Path:
    return Path(lakehouse_root) / "Files" / "_weaver" / "logs" / "rejects"


def write_rejects(lakehouse_root: Path, object_id: str, rejected: Sequence[dict]) -> str | None:
    """Write rejected rows to a per-object reject artifact; return its path."""

    if not rejected:
        return None
    directory = rejects_root(lakehouse_root) / object_id
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "rejects.json"
    path.write_text(json.dumps(list(rejected), indent=2, default=str), encoding="utf-8")
    return str(path)
