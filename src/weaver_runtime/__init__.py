"""Runtime helpers for mirrored Fabric platform notebooks."""

from __future__ import annotations

from pathlib import Path
from typing import Any


def hello_report(platform_root: str | Path) -> dict[str, Any]:
    """Return a small import/runtime report for the mirrored platform."""

    root = Path(platform_root)
    return {
        "platform_root": str(root),
        "platform_root_exists": root.is_dir(),
        "module_path": __file__,
    }
