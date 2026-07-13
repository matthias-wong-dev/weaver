"""General local-folder -> Lakehouse Files folder sync (no Git required).

Filtered folder sync with content signatures. Deletion, when enabled, is scoped
strictly to the configured target folder; the Lakehouse ``Files/`` root is never
destructively synced.
"""

from __future__ import annotations

import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import onelake
from .client import FabricClientError
from .ignore import IgnoreSpec, load_ignore_spec, parse_ignore_lines
from .onelake import LakehouseTarget, OneLakeError
from .settings import DEFAULT_DEGREES_OF_PARALLELISM

SIGNATURES_NAME = "signatures.json"
SIGNATURES_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class FileSnapshot:
    """One local file's content and sync signature."""

    relative_path: str
    content: bytes
    signature: dict[str, Any]


@dataclass(frozen=True)
class FolderDiff:
    upload_paths: list[str]
    delete_paths: list[str]
    write_signatures: bool


def snapshot_folder(
    root: Path,
    *,
    respect_ignore: bool = True,
    extra_ignore: IgnoreSpec | None = None,
    ignored_out: list[str] | None = None,
) -> dict[str, FileSnapshot]:
    """Read all non-ignored files under ``root`` and compute signatures.

    When ``ignored_out`` is provided, every file skipped by ``.weaverignore`` or
    the baseline is appended to it, so callers can report what was excluded.
    """

    root = root.resolve()
    if not root.exists():
        raise OneLakeError(f"source folder not found: {root}")
    if not root.is_dir():
        raise OneLakeError(f"source is not a directory: {root}")

    spec = load_ignore_spec(root) if respect_ignore else parse_ignore_lines([])
    snapshots: dict[str, FileSnapshot] = {}

    def _ignored(rel: str, *, is_dir: bool) -> bool:
        if respect_ignore and spec.match(rel, is_dir=is_dir):
            return True
        if extra_ignore is not None and extra_ignore.match(rel, is_dir=is_dir):
            return True
        return False

    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        rel_dir = os.path.relpath(dirpath, root)
        rel_dir = "" if rel_dir == "." else rel_dir.replace(os.sep, "/")
        # Prune ignored directories in place so os.walk does not descend into them.
        kept = []
        for name in sorted(dirnames):
            rel = f"{rel_dir}/{name}" if rel_dir else name
            if _ignored(rel, is_dir=True):
                if ignored_out is not None:
                    ignored_out.append(f"{rel}/")
                continue
            kept.append(name)
        dirnames[:] = kept

        for name in sorted(filenames):
            rel = f"{rel_dir}/{name}" if rel_dir else name
            if rel == SIGNATURES_NAME:
                continue
            if _ignored(rel, is_dir=False):
                if ignored_out is not None:
                    ignored_out.append(rel)
                continue
            path = Path(dirpath) / name
            if path.is_symlink() or not path.is_file():
                continue
            relative_path = onelake.validate_relative_path(rel)
            content = path.read_bytes()
            snapshots[relative_path] = FileSnapshot(
                relative_path=relative_path,
                content=content,
                signature={
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "size": len(content),
                },
            )
    return snapshots


def signatures_document(signatures: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return {
        "schema_version": SIGNATURES_SCHEMA_VERSION,
        "files": {path: signatures[path] for path in sorted(signatures)},
    }


def extract_remote_signatures(payload: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    if payload is None:
        return {}
    if not isinstance(payload, dict) or payload.get("schema_version") != SIGNATURES_SCHEMA_VERSION:
        raise OneLakeError(f"unsupported remote {SIGNATURES_NAME}")
    files = payload.get("files")
    if not isinstance(files, dict):
        raise OneLakeError(f"remote {SIGNATURES_NAME} files must be an object")
    signatures: dict[str, dict[str, Any]] = {}
    for raw_path, signature in files.items():
        if not isinstance(raw_path, str) or not isinstance(signature, dict):
            raise OneLakeError(f"remote {SIGNATURES_NAME} has a malformed entry")
        signatures[onelake.validate_relative_path(raw_path)] = dict(signature)
    return signatures


def calculate_diff(
    local_signatures: dict[str, dict[str, Any]],
    remote_signatures: dict[str, dict[str, Any]],
    remote_payload_paths: set[str],
    *,
    delete: bool = True,
) -> FolderDiff:
    """Calculate uploads/deletes and whether the signatures doc must be rewritten."""

    local_paths = set(local_signatures)
    upload_paths = sorted(
        path
        for path, signature in local_signatures.items()
        if path not in remote_payload_paths or remote_signatures.get(path) != signature
    )
    delete_paths = sorted(remote_payload_paths - local_paths) if delete else []
    write_signatures = bool(
        upload_paths or delete_paths or remote_signatures != local_signatures
    )
    return FolderDiff(
        upload_paths=upload_paths,
        delete_paths=delete_paths,
        write_signatures=write_signatures,
    )


def _read_remote_signatures(target: LakehouseTarget, folder: str) -> dict[str, dict[str, Any]]:
    metadata_path = "/".join(part for part in [folder, SIGNATURES_NAME] if part)
    try:
        raw = onelake.read_file(target, metadata_path)
    except FabricClientError as exc:
        # A fresh folder has no signatures file yet: treat 404 as "no signatures".
        if "returned HTTP 404" in str(exc):
            return {}
        raise
    try:
        payload = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise OneLakeError(f"remote {metadata_path} is not valid JSON: {exc}") from exc
    return extract_remote_signatures(payload)


def _list_remote_payload_paths(target: LakehouseTarget, folder: str) -> set[str]:
    try:
        remote_files = onelake.list_files(target, folder)
    except FabricClientError as exc:
        # A fresh/absent remote folder lists as 404: treat as "no remote files".
        if "returned HTTP 404" in str(exc):
            return set()
        raise
    return onelake.relative_payload_paths(
        remote_files, target.lakehouse_id, folder, reserved={SIGNATURES_NAME}
    )


def sync_folder(
    target: LakehouseTarget,
    source_dir: Path,
    target_folder: str,
    *,
    respect_ignore: bool = True,
    signatures: bool = True,
    delete: bool = True,
    degrees_of_parallelism: int = DEFAULT_DEGREES_OF_PARALLELISM,
    dry_run: bool = False,
    extra_ignore: IgnoreSpec | None = None,
) -> dict[str, Any]:
    """Reconcile one local folder with one Lakehouse Files target folder."""

    source_dir = Path(source_dir).resolve()
    folder = onelake.normalise_files_folder(target_folder)
    ignored: list[str] = []
    snapshots = snapshot_folder(
        source_dir,
        respect_ignore=respect_ignore,
        extra_ignore=extra_ignore,
        ignored_out=ignored,
    )
    local_signatures = {path: snap.signature for path, snap in snapshots.items()}

    if dry_run:
        return {
            "operation": "fabric.onelake.sync",
            "source": str(source_dir),
            "target_folder": f"Files/{folder}" if folder else "Files",
            "files": {
                "local": len(snapshots),
                "uploaded": 0,
                "deleted": 0,
                "unchanged": len(snapshots),
                "ignored": len(ignored),
            },
            "paths": sorted(snapshots),
            "ignored_paths": sorted(ignored),
            "degrees_of_parallelism": degrees_of_parallelism,
            "signatures": signatures,
            "respect_ignore": respect_ignore,
            "delete": delete,
            "dry_run": True,
            "success": True,
        }

    remote_signatures = _read_remote_signatures(target, folder) if signatures else {}
    remote_payload_paths = _list_remote_payload_paths(target, folder)
    diff = calculate_diff(
        local_signatures, remote_signatures, remote_payload_paths, delete=delete
    )

    upload_directories = {folder} if folder and diff.upload_paths else set()
    upload_directories.update(
        "/".join(
            part
            for part in [folder, Path(relative).parent.as_posix()]
            if part
        )
        for relative in diff.upload_paths
        if str(Path(relative).parent) != "."
    )
    for directory in sorted(upload_directories):
        onelake.ensure_directory(target, directory)

    def upload_one(relative: str) -> str:
        snap = snapshots[relative]
        target_path = "/".join(part for part in [folder, relative] if part)
        onelake.upload_file(target, target_path, snap.content, ensure_parent=False)
        return relative

    def delete_one(relative: str) -> str:
        target_path = "/".join(part for part in [folder, relative] if part)
        onelake.delete_file(target, target_path)
        return relative

    uploaded: list[str] = []
    deleted: list[str] = []
    workers = max(1, degrees_of_parallelism)
    with ThreadPoolExecutor(max_workers=workers) as executor:
        for future in as_completed(
            {executor.submit(upload_one, path): path for path in diff.upload_paths}
        ):
            uploaded.append(future.result())
        for future in as_completed(
            {executor.submit(delete_one, path): path for path in diff.delete_paths}
        ):
            deleted.append(future.result())

    removed_directories = (
        remove_empty_directories(target, folder) if delete and diff.delete_paths else []
    )

    wrote_signatures = False
    if signatures and diff.write_signatures:
        metadata_path = "/".join(part for part in [folder, SIGNATURES_NAME] if part)
        onelake.upload_file(
            target,
            metadata_path,
            json.dumps(signatures_document(local_signatures), indent=2, sort_keys=True).encode(
                "utf-8"
            )
            + b"\n",
        )
        wrote_signatures = True

    return {
        "operation": "fabric.onelake.sync",
        "source": str(source_dir),
        "target_folder": f"Files/{folder}" if folder else "Files",
        "files": {
            "local": len(snapshots),
            "uploaded": len(uploaded),
            "deleted": len(deleted),
            "unchanged": len(snapshots) - len(diff.upload_paths),
            "ignored": len(ignored),
        },
        "uploaded_paths": sorted(uploaded),
        "deleted_paths": sorted(deleted),
        "deleted_directories": removed_directories,
        "ignored_sample": sorted(ignored)[:20],
        "degrees_of_parallelism": degrees_of_parallelism,
        "signatures": bool(signatures),
        "signatures_written": wrote_signatures,
        "respect_ignore": respect_ignore,
        "delete": delete,
        "success": True,
    }


def remove_empty_directories(target: LakehouseTarget, folder: str) -> list[str]:
    """Remove empty descendants of ``folder`` without deleting ``folder`` itself.

    OneLake directory metadata determines emptiness; file length is deliberately
    ignored so a legitimate zero-byte file keeps its parent directory alive.
    """

    root = onelake.normalise_files_folder(folder)
    try:
        paths = onelake.list_paths(target, root)
    except FabricClientError as exc:
        if "returned HTTP 404" in str(exc):
            return []
        raise

    prefix = "/".join(
        part
        for part in [onelake.artifact_path(target.lakehouse_id), "Files", root]
        if part
    )
    prefix_with_slash = f"{prefix}/"
    directories: set[str] = set()
    files: set[str] = set()
    for entry in paths:
        name = str(entry.get("name", "")).strip("/")
        if not name.startswith(prefix_with_slash):
            if name == prefix:
                continue
            raise OneLakeError(f"unexpected remote path outside target: {name}")
        relative = onelake.validate_relative_path(name[len(prefix_with_slash) :])
        (directories if onelake.path_is_directory(entry) else files).add(relative)

    occupied: set[str] = set()
    for file_path in files:
        parts = file_path.split("/")[:-1]
        occupied.update("/".join(parts[:index]) for index in range(1, len(parts) + 1))

    empty = sorted(
        directories - occupied,
        key=lambda path: (-path.count("/"), path),
    )
    removed: list[str] = []
    for relative in empty:
        # ``relative`` was validated and ``root`` was normalised above, so this
        # delete cannot address the component root or escape its descendants.
        path = "/".join(part for part in ["Files", root, relative] if part)
        onelake.delete_directory(target, path)
        removed.append(relative)
    return removed
