"""Target-only load orchestrator.

Given an installed runtime root, the orchestrator:

1. Reads ``manifest.json``, ``load_plan.json``, ``source_hashes.json``.
2. Discovers database folders and object files structurally (ignoring ``_`` names).
3. Validates discovery against the manifest (presence + source hashes).
4. Runs steps in the installed load-plan order (when executing with Spark).

It never reads the original SES repo and never requires ``--from``.
"""

from __future__ import annotations

from pathlib import Path

from ..build.manifest import read_json, source_hash
from ..errors import LoadError
from ..ses.discovery import discover_runtime_objects
from .logging import LoadReport, StepLog

MANIFEST_NAME = "manifest.json"
LOAD_PLAN_NAME = "load_plan.json"
SOURCE_HASHES_NAME = "source_hashes.json"


def load_target_runtime(
    runtime_root,
    *,
    execute: bool = True,
    object_filter: tuple[str, ...] | None = None,
    include_static: bool = False,
    strict: bool = True,
    spark=None,
) -> LoadReport:
    """Load a target from its installed runtime bundle."""

    root = Path(runtime_root)
    if not root.is_dir():
        raise LoadError(f"runtime root does not exist: {root}")

    manifest = _read(root, MANIFEST_NAME)
    load_plan = _read(root, LOAD_PLAN_NAME)
    hashes = _read(root, SOURCE_HASHES_NAME)

    discovered = {obj.id: obj for obj in discover_runtime_objects(root)}
    _validate_against_manifest(discovered, manifest, hashes, strict=strict)

    steps = [
        step
        for step in load_plan.get("steps", [])
        if object_filter is None or step["object"] in object_filter
    ]

    if not execute:
        return LoadReport(
            runtime_root=str(root),
            executed=False,
            ok=True,
            steps=tuple(
                StepLog(object_id=step["object"], kind=step["kind"], status="planned")
                for step in steps
            ),
            message="validated installed runtime (not executed)",
        )

    # Execution requires Spark; imported lazily so core stays PySpark-free.
    from .load import execute_load_plan

    return execute_load_plan(
        runtime_root=root,
        manifest=manifest,
        load_plan=load_plan,
        discovered=discovered,
        steps=steps,
        include_static=include_static,
        spark=spark,
    )


def _read(root: Path, name: str) -> dict:
    path = root / name
    if not path.is_file():
        raise LoadError(f"installed runtime is missing {name}: {path}")
    return read_json(path)


def _validate_against_manifest(discovered, manifest, hashes, *, strict: bool) -> None:
    manifest_ids = {entry["id"] for entry in manifest.get("objects", [])}

    missing = sorted(manifest_ids - set(discovered))
    if missing:
        raise LoadError(
            "installed runtime is missing manifest objects: " + ", ".join(missing)
        )

    unknown = sorted(set(discovered) - manifest_ids)
    if unknown and strict:
        raise LoadError(
            "installed runtime has objects not in the manifest: " + ", ".join(unknown)
        )

    for object_id in sorted(manifest_ids):
        expected = hashes.get(object_id)
        if expected is None:
            raise LoadError(f"source hash missing for {object_id}")
        actual = source_hash(discovered[object_id].text)
        if actual != expected:
            raise LoadError(
                f"source hash mismatch for {object_id}: installed runtime does not "
                "match recorded source"
            )
