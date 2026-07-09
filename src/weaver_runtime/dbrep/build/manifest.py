"""Runtime artifacts: manifest, load plan, and source hashes.

These pure builders turn planned objects into the JSON documents installed under
``Files/_weaver/runtime``. The manifest is the source of truth for target-only
load; the load plan is topologically ordered; source hashes detect drift.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Sequence

from ..config.resolution import RUNTIME_RELATIVE_ROOT
from ..ses.metadata import FOLDER, TABLE

FOLDER_ACTION = "run_load"
TABLE_ACTION = "run_read_and_apply_policy"


def weaver_version() -> str:
    try:
        from importlib.metadata import version

        return version("weaver-runtime")
    except Exception:  # pragma: no cover - metadata always present when installed
        return "0+unknown"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def source_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def build_manifest(
    objects: Sequence,
    *,
    target_server: str,
    installed_from: Iterable[str],
    installed_to: Iterable[str],
    external_dependencies: Iterable,
    installed_at: str | None = None,
    version: str | None = None,
    runtime_root: str = RUNTIME_RELATIVE_ROOT,
) -> dict:
    """Build the manifest document for one runtime host."""

    return {
        "target_server": target_server,
        "runtime_root": runtime_root,
        "installed_at": installed_at or utc_now_iso(),
        "weaver_version": version or weaver_version(),
        "installed_from": sorted(set(installed_from)),
        "installed_to": sorted(set(installed_to)),
        "objects": [_manifest_object(planned) for planned in objects],
        "external_dependencies": [
            {"id": external.id, "reason": external.reason}
            for external in external_dependencies
        ],
    }


def _manifest_object(planned) -> dict:
    metadata = planned.source.metadata
    return {
        "id": planned.id,
        "declared_as": planned.declared_as,
        "kind": planned.kind,
        "source_database": planned.source_alias,
        "target_database": planned.target_alias,
        "materialisation": planned.materialisation,
        "primary_key": list(metadata.primary_key),
        "auto_delete": metadata.auto_delete,
        "static": metadata.static,
        "load_mode": metadata.effective_load_mode,
        "language": planned.source.language,
        "dependencies": [
            {"id": dependency.id, "scope": dependency.scope}
            for dependency in planned.dependencies
        ],
    }


def build_load_plan(
    objects_in_order: Sequence,
    *,
    server: str,
    targets: Iterable[str],
    runtime_root: str = RUNTIME_RELATIVE_ROOT,
) -> dict:
    """Build the topologically ordered load plan for one runtime host."""

    steps = []
    for planned in objects_in_order:
        if planned.kind == FOLDER:
            action = FOLDER_ACTION
        elif planned.kind == TABLE:
            action = TABLE_ACTION
        else:
            continue
        steps.append({"object": planned.id, "kind": planned.kind, "action": action})

    return {
        "server": server,
        "targets": sorted(set(targets)),
        "runtime_root": runtime_root,
        "steps": steps,
    }


def build_source_hashes(objects: Sequence) -> dict:
    """Map object id -> sha256 of its source file text."""

    return {planned.id: source_hash(planned.source.text) for planned in objects}


def write_json(path: Path, document: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")


def read_json(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))
