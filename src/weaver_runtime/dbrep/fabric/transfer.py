"""Internal OneLake tree transfer used only by the DBRep Fabric backend."""

from __future__ import annotations

import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ...fabric import onelake
from ...fabric.client import FabricClientError
from ...fabric.onelake import LakehouseTarget, OneLakeError

SIGNATURES_NAME = "signatures.json"
SIGNATURES_SCHEMA_VERSION = 1
_IGNORED_DIRS = {
    ".git",
    ".ipynb_checkpoints",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "node_modules",
    "venv",
}
_IGNORED_FILES = {".DS_Store", ".env"}
_IGNORED_SUFFIXES = {".pyc", ".pyo"}


@dataclass(frozen=True)
class FileSnapshot:
    relative_path: str
    content: bytes
    signature: dict[str, Any]


@dataclass(frozen=True)
class TreeDiff:
    upload_paths: list[str]
    delete_paths: list[str]
    write_signatures: bool


def snapshot_tree(root: Path) -> dict[str, FileSnapshot]:
    """Snapshot a generated DBRep tree, excluding runtime/tooling cache files."""

    root = root.resolve()
    if not root.exists():
        raise OneLakeError(f"DBRep transfer source not found: {root}")
    if not root.is_dir():
        raise OneLakeError(f"DBRep transfer source is not a directory: {root}")

    snapshots: dict[str, FileSnapshot] = {}
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(name for name in dirnames if name not in _IGNORED_DIRS)
        for name in sorted(filenames):
            if (
                name == SIGNATURES_NAME
                or name in _IGNORED_FILES
                or Path(name).suffix in _IGNORED_SUFFIXES
            ):
                continue
            path = Path(dirpath) / name
            if path.is_symlink() or not path.is_file():
                continue
            relative = onelake.validate_relative_path(
                path.relative_to(root).as_posix()
            )
            content = path.read_bytes()
            snapshots[relative] = FileSnapshot(
                relative_path=relative,
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
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != SIGNATURES_SCHEMA_VERSION
    ):
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
    delete: bool,
) -> TreeDiff:
    local_paths = set(local_signatures)
    upload_paths = sorted(
        path
        for path, signature in local_signatures.items()
        if path not in remote_payload_paths or remote_signatures.get(path) != signature
    )
    delete_paths = sorted(remote_payload_paths - local_paths) if delete else []
    return TreeDiff(
        upload_paths=upload_paths,
        delete_paths=delete_paths,
        write_signatures=bool(
            upload_paths or delete_paths or remote_signatures != local_signatures
        ),
    )


def _read_remote_signatures(
    target: LakehouseTarget, folder: str
) -> dict[str, dict[str, Any]]:
    metadata_path = "/".join(part for part in (folder, SIGNATURES_NAME) if part)
    try:
        raw = onelake.read_file(target, metadata_path)
    except FabricClientError as exc:
        if "returned HTTP 404" in str(exc):
            return {}
        raise
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OneLakeError(f"remote {metadata_path} is not valid JSON: {exc}") from exc
    return extract_remote_signatures(payload)


def _list_remote_payload_paths(target: LakehouseTarget, folder: str) -> set[str]:
    try:
        remote_files = onelake.list_files(target, folder)
    except FabricClientError as exc:
        if "returned HTTP 404" in str(exc):
            return set()
        raise
    return onelake.relative_payload_paths(
        remote_files, target.lakehouse_id, folder, reserved={SIGNATURES_NAME}
    )


def sync_tree(
    target: LakehouseTarget,
    source_dir: Path,
    target_folder: str,
    *,
    delete: bool,
    degrees_of_parallelism: int,
) -> dict[str, Any]:
    """Reconcile one generated DBRep tree with its owned OneLake folder."""

    source_dir = Path(source_dir).resolve()
    folder = onelake.normalise_files_folder(target_folder)
    snapshots = snapshot_tree(source_dir)
    local_signatures = {path: snap.signature for path, snap in snapshots.items()}
    remote_signatures = _read_remote_signatures(target, folder)
    remote_payload_paths = _list_remote_payload_paths(target, folder)
    diff = calculate_diff(
        local_signatures,
        remote_signatures,
        remote_payload_paths,
        delete=delete,
    )

    directories = {folder} if folder and diff.upload_paths else set()
    directories.update(
        "/".join(part for part in (folder, Path(relative).parent.as_posix()) if part)
        for relative in diff.upload_paths
        if Path(relative).parent.as_posix() != "."
    )
    for directory in sorted(directories):
        onelake.ensure_directory(target, directory)

    def upload_one(relative: str) -> str:
        target_path = "/".join(part for part in (folder, relative) if part)
        onelake.upload_file(
            target, target_path, snapshots[relative].content, ensure_parent=False
        )
        return relative

    def delete_one(relative: str) -> str:
        target_path = "/".join(part for part in (folder, relative) if part)
        onelake.delete_file(target, target_path)
        return relative

    uploaded: list[str] = []
    deleted: list[str] = []
    with ThreadPoolExecutor(max_workers=max(1, degrees_of_parallelism)) as executor:
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
    if diff.write_signatures:
        metadata_path = "/".join(part for part in (folder, SIGNATURES_NAME) if part)
        document = json.dumps(
            signatures_document(local_signatures), indent=2, sort_keys=True
        ).encode("utf-8") + b"\n"
        onelake.upload_file(target, metadata_path, document)

    return {
        "operation": "dbrep.fabric.transfer",
        "source": str(source_dir),
        "target_folder": f"Files/{folder}" if folder else "Files",
        "files": {
            "local": len(snapshots),
            "uploaded": len(uploaded),
            "deleted": len(deleted),
            "unchanged": len(snapshots) - len(diff.upload_paths),
        },
        "uploaded_paths": sorted(uploaded),
        "deleted_paths": sorted(deleted),
        "deleted_directories": removed_directories,
        "degrees_of_parallelism": degrees_of_parallelism,
        "signatures_written": diff.write_signatures,
        "delete": delete,
        "success": True,
    }


def remove_empty_directories(target: LakehouseTarget, folder: str) -> list[str]:
    """Remove empty descendants without deleting the DBRep-owned root."""

    root = onelake.normalise_files_folder(folder)
    try:
        paths = onelake.list_paths(target, root)
    except FabricClientError as exc:
        if "returned HTTP 404" in str(exc):
            return []
        raise

    prefix = "/".join(
        part
        for part in (onelake.artifact_path(target.lakehouse_id), "Files", root)
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

    empty = sorted(directories - occupied, key=lambda path: (-path.count("/"), path))
    for relative in empty:
        path = "/".join(part for part in ("Files", root, relative) if part)
        onelake.delete_directory(target, path)
    return empty
