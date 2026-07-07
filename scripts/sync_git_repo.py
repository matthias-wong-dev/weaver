#!/usr/bin/env python3
"""Mirror one local Git working tree into a Fabric Lakehouse Files folder."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from azure.identity import DefaultAzureCredential

from sync_folder import (
    DEFAULT_API_BASE_URL,
    DEFAULT_FABRIC_SCOPE,
    DEFAULT_LAKEHOUSE_NAME,
    DEFAULT_ONELAKE_BASE_URL,
    DEFAULT_STORAGE_SCOPE,
    DEFAULT_WORKSPACE_NAME,
    SyncError,
    find_workspace_id,
    list_onelake_files,
    onelake_file_url,
    read_onelake_file,
    request_bytes,
    resolve_lakehouse,
    ensure_onelake_directory,
    upload_onelake_file,
    workspace_display_name,
)


SIGNATURES_NAME = "signatures.json"
SIGNATURES_SCHEMA_VERSION = 1
DEFAULT_WORKERS = 32


class GitRepoSyncError(RuntimeError):
    """Raised when Git-aware Lakehouse sync fails."""


@dataclass(frozen=True)
class LocalFileSnapshot:
    """One local file's content and sync signature."""

    relative_path: str
    content: bytes
    signature: dict[str, Any]


@dataclass(frozen=True)
class RepoDiff:
    """Payload changes needed to reconcile one remote mirror."""

    upload_paths: list[str]
    delete_paths: list[str]
    write_signatures: bool


def normalise_files_folder(path: str) -> str:
    """Return a Lakehouse Files-relative folder path."""

    normalised = path.strip().strip("/")
    if normalised == "Files":
        return ""
    if normalised.startswith("Files/"):
        normalised = normalised[len("Files/") :]
    parts = [part for part in normalised.split("/") if part]
    if any(part in {".", ".."} for part in parts):
        raise GitRepoSyncError(f"unsafe target folder: {path!r}")
    return "/".join(parts)


def validate_relative_path(raw_path: str) -> str:
    """Validate a Git path before using it as a Lakehouse payload path."""

    path = raw_path.replace(os.sep, "/")
    parts = path.split("/")
    if (
        not path
        or path.startswith("/")
        or "\\" in path
        or any(part in {"", ".", ".."} for part in parts)
    ):
        raise GitRepoSyncError(f"unsafe Git path: {raw_path!r}")
    if path == SIGNATURES_NAME:
        raise GitRepoSyncError(
            f"{SIGNATURES_NAME} is reserved for Fabric runtime sync metadata"
        )
    return path


def run_git(repo_root: Path, args: list[str]) -> str:
    """Run a Git command in the source repository."""

    completed = subprocess.run(
        ["git", "-C", str(repo_root), *args],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if completed.returncode:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise GitRepoSyncError(f"git {' '.join(args)} failed in {repo_root}: {detail}")
    return completed.stdout.decode("utf-8", errors="surrogateescape")


def git_toplevel(repo_root: Path) -> Path:
    """Return the Git top-level path for a working tree."""

    return Path(run_git(repo_root, ["rev-parse", "--show-toplevel"]).strip()).resolve()


def git_visible_files(repo_root: Path) -> list[str]:
    """Return tracked and non-ignored untracked files in the Git working tree."""

    repo_root = repo_root.resolve()
    top_level = git_toplevel(repo_root)
    if top_level != repo_root:
        raise GitRepoSyncError(
            f"source must be a Git working tree root: {repo_root} (top-level is {top_level})"
        )

    raw = subprocess.run(
        [
            "git",
            "-C",
            str(repo_root),
            "ls-files",
            "-z",
            "--cached",
            "--others",
            "--exclude-standard",
        ],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if raw.returncode:
        detail = raw.stderr.decode("utf-8", errors="replace").strip()
        raise GitRepoSyncError(f"git ls-files failed in {repo_root}: {detail}")

    files: list[str] = []
    for item in raw.stdout.decode("utf-8", errors="surrogateescape").split("\0"):
        if not item:
            continue
        relative_path = validate_relative_path(item)
        file_path = repo_root / Path(relative_path)
        if file_path.is_file():
            if file_path.is_symlink():
                raise GitRepoSyncError(f"symlink payloads are not supported: {relative_path}")
            files.append(relative_path)

    return sorted(set(files))


def snapshot_git_visible_files(repo_root: Path) -> dict[str, LocalFileSnapshot]:
    """Read all Git-visible files and calculate content signatures."""

    snapshots: dict[str, LocalFileSnapshot] = {}
    for relative_path in git_visible_files(repo_root):
        content = (repo_root / Path(relative_path)).read_bytes()
        snapshots[relative_path] = LocalFileSnapshot(
            relative_path=relative_path,
            content=content,
            signature={
                "sha256": hashlib.sha256(content).hexdigest(),
                "size": len(content),
            },
        )
    return snapshots


def signatures_document(signatures: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Return the deterministic remote signatures document."""

    return {
        "schema_version": SIGNATURES_SCHEMA_VERSION,
        "files": {path: signatures[path] for path in sorted(signatures)},
    }


def extract_remote_signatures(payload: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    """Return remote payload file signatures from the control document."""

    if payload is None:
        return {}
    if not isinstance(payload, dict):
        raise GitRepoSyncError(f"remote {SIGNATURES_NAME} must be a JSON object")
    if payload.get("schema_version") != SIGNATURES_SCHEMA_VERSION:
        raise GitRepoSyncError(
            f"unsupported remote {SIGNATURES_NAME} schema_version: "
            f"{payload.get('schema_version')!r}"
        )
    files = payload.get("files")
    if not isinstance(files, dict):
        raise GitRepoSyncError(f"remote {SIGNATURES_NAME} files must be an object")

    signatures: dict[str, dict[str, Any]] = {}
    for raw_path, signature in files.items():
        if not isinstance(raw_path, str):
            raise GitRepoSyncError(f"remote {SIGNATURES_NAME} contains a non-string path")
        path = validate_relative_path(raw_path)
        if not isinstance(signature, dict):
            raise GitRepoSyncError(f"remote signature for {path} must be an object")
        signatures[path] = dict(signature)
    return signatures


def calculate_diff(
    local_signatures: dict[str, dict[str, Any]],
    remote_signatures: dict[str, dict[str, Any]],
    remote_payload_paths: set[str],
) -> RepoDiff:
    """Calculate payload uploads/deletes and whether metadata must be rewritten."""

    local_paths = set(local_signatures)
    upload_paths = sorted(
        path
        for path, signature in local_signatures.items()
        if path not in remote_payload_paths or remote_signatures.get(path) != signature
    )
    delete_paths = sorted(remote_payload_paths - local_paths)
    write_signatures = bool(
        upload_paths
        or delete_paths
        or remote_signatures != local_signatures
    )
    return RepoDiff(
        upload_paths=upload_paths,
        delete_paths=delete_paths,
        write_signatures=write_signatures,
    )


def remote_relative_paths(
    remote_files: list[dict[str, Any]],
    lakehouse_id: str,
    target_folder: str,
) -> set[str]:
    """Convert OneLake list results into paths relative to the mirror target."""

    from sync_folder import onelake_artifact_path

    remote_prefix = "/".join(
        part
        for part in [
            onelake_artifact_path(lakehouse_id),
            "Files",
            *[part for part in target_folder.strip("/").split("/") if part],
        ]
        if part
    )
    remote_prefix_with_slash = f"{remote_prefix}/"
    paths: set[str] = set()

    for remote_file in remote_files:
        remote_name = str(remote_file.get("name", "")).strip("/")
        if remote_name == remote_prefix:
            relative_path = Path(remote_name).name
        elif remote_name.startswith(remote_prefix_with_slash):
            relative_path = remote_name[len(remote_prefix_with_slash) :]
        else:
            raise GitRepoSyncError(f"unexpected remote file outside target: {remote_name}")
        if relative_path == SIGNATURES_NAME:
            continue
        paths.add(validate_relative_path(relative_path))

    return paths


def read_remote_signatures(
    token: str,
    onelake_base_url: str,
    workspace_id: str,
    lakehouse_id: str,
    target_folder: str,
) -> dict[str, dict[str, Any]]:
    """Read remote sync metadata, returning an empty map when it is absent."""

    metadata_path = "/".join(part for part in [target_folder, SIGNATURES_NAME] if part)
    try:
        raw = read_onelake_file(
            token=token,
            onelake_base_url=onelake_base_url,
            workspace_id=workspace_id,
            lakehouse_id=lakehouse_id,
            files_path=metadata_path,
        )
    except SyncError as exc:
        if "returned HTTP 404" in str(exc):
            return {}
        raise

    try:
        payload = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise GitRepoSyncError(f"remote {metadata_path} is not valid JSON: {exc}") from exc
    return extract_remote_signatures(payload)


def list_remote_payload_paths(
    token: str,
    onelake_base_url: str,
    workspace_id: str,
    lakehouse_id: str,
    target_folder: str,
) -> set[str]:
    """List actual remote payload files under the target mirror folder."""

    try:
        remote_files = list_onelake_files(
            token=token,
            onelake_base_url=onelake_base_url,
            workspace_id=workspace_id,
            lakehouse_id=lakehouse_id,
            lakehouse_folder=target_folder,
        )
    except SyncError as exc:
        if "returned HTTP 404" in str(exc):
            return set()
        raise
    return remote_relative_paths(remote_files, lakehouse_id, target_folder)


def delete_onelake_file(
    token: str,
    onelake_base_url: str,
    workspace_id: str,
    lakehouse_id: str,
    files_path: str,
) -> None:
    """Delete one Lakehouse Files object when it exists."""

    request_bytes(
        "DELETE",
        onelake_file_url(onelake_base_url, workspace_id, lakehouse_id, files_path),
        token,
        headers={"x-ms-version": "2023-11-03"},
        expected_statuses={200, 202, 204, 404},
    )


def resolve_fabric_target(args: argparse.Namespace) -> tuple[str, str | None, dict[str, Any], str]:
    """Resolve workspace/lakehouse IDs and return a OneLake storage token."""

    credential = DefaultAzureCredential()
    fabric_token = credential.get_token(args.fabric_scope).token
    workspace_id = args.workspace_id or find_workspace_id(
        fabric_token,
        args.workspace_name,
        args.api_base_url,
    )
    workspace_name = workspace_display_name(fabric_token, workspace_id, args.api_base_url)
    lakehouse = resolve_lakehouse(
        token=fabric_token,
        workspace_id=workspace_id,
        lakehouse_id=args.lakehouse_id,
        lakehouse_name=args.lakehouse_name,
        api_base_url=args.api_base_url,
    )
    storage_token = credential.get_token(args.storage_scope).token
    return workspace_id, workspace_name, lakehouse, storage_token


def sync_repository(
    source_dir: Path,
    target_folder: str,
    *,
    workspace_id: str,
    lakehouse_id: str,
    storage_token: str,
    onelake_base_url: str = DEFAULT_ONELAKE_BASE_URL,
    workers: int = DEFAULT_WORKERS,
    dry_run: bool = False,
    include_signatures: bool = False,
) -> dict[str, Any]:
    """Reconcile one local Git repo with one Lakehouse target folder."""

    source_dir = source_dir.resolve()
    target_folder = normalise_files_folder(target_folder)
    snapshots = snapshot_git_visible_files(source_dir)
    local_signatures = {
        path: snapshot.signature
        for path, snapshot in snapshots.items()
    }

    if dry_run:
        result: dict[str, Any] = {
            "source": str(source_dir),
            "target": f"Files/{target_folder}",
            "local_files": len(snapshots),
            "paths": sorted(snapshots),
            "dry_run": True,
            "success": True,
        }
        if include_signatures:
            result["signatures"] = signatures_document(local_signatures)
        return result

    remote_signatures = read_remote_signatures(
        token=storage_token,
        onelake_base_url=onelake_base_url,
        workspace_id=workspace_id,
        lakehouse_id=lakehouse_id,
        target_folder=target_folder,
    )
    remote_payload_paths = list_remote_payload_paths(
        token=storage_token,
        onelake_base_url=onelake_base_url,
        workspace_id=workspace_id,
        lakehouse_id=lakehouse_id,
        target_folder=target_folder,
    )
    diff = calculate_diff(local_signatures, remote_signatures, remote_payload_paths)

    uploaded: list[dict[str, Any]] = []
    deleted: list[str] = []
    upload_directories = {target_folder} if target_folder and diff.upload_paths else set()
    upload_directories.update(
        "/".join(
            part
            for part in [
                target_folder,
                Path(relative_path).parent.as_posix(),
            ]
            if part
        )
        for relative_path in diff.upload_paths
        if str(Path(relative_path).parent) != "."
    )
    for directory in sorted(upload_directories):
        ensure_onelake_directory(
            token=storage_token,
            onelake_base_url=onelake_base_url,
            workspace_id=workspace_id,
            lakehouse_id=lakehouse_id,
            directory_path=directory,
        )

    def upload_one(relative_path: str) -> dict[str, Any]:
        snapshot = snapshots[relative_path]
        target_path = "/".join(part for part in [target_folder, relative_path] if part)
        upload_onelake_file(
            token=storage_token,
            onelake_base_url=onelake_base_url,
            workspace_id=workspace_id,
            lakehouse_id=lakehouse_id,
            files_path=target_path,
            content=snapshot.content,
            ensure_parent=False,
        )
        return {
            "path": relative_path,
            "target_path": f"Files/{target_path}",
            **snapshot.signature,
        }

    def delete_one(relative_path: str) -> str:
        target_path = "/".join(part for part in [target_folder, relative_path] if part)
        delete_onelake_file(
            token=storage_token,
            onelake_base_url=onelake_base_url,
            workspace_id=workspace_id,
            lakehouse_id=lakehouse_id,
            files_path=target_path,
        )
        return relative_path

    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        upload_futures = {
            executor.submit(upload_one, relative_path): relative_path
            for relative_path in diff.upload_paths
        }
        for future in as_completed(upload_futures):
            uploaded.append(future.result())

        delete_futures = {
            executor.submit(delete_one, relative_path): relative_path
            for relative_path in diff.delete_paths
        }
        for future in as_completed(delete_futures):
            deleted.append(future.result())

    wrote_signatures = False
    if diff.write_signatures:
        metadata_path = "/".join(part for part in [target_folder, SIGNATURES_NAME] if part)
        upload_onelake_file(
            token=storage_token,
            onelake_base_url=onelake_base_url,
            workspace_id=workspace_id,
            lakehouse_id=lakehouse_id,
            files_path=metadata_path,
            content=json.dumps(
                signatures_document(local_signatures),
                indent=2,
                sort_keys=True,
            ).encode("utf-8")
            + b"\n",
        )
        wrote_signatures = True

    return {
        "source": str(source_dir),
        "target": f"Files/{target_folder}",
        "local_files": len(snapshots),
        "remote_files": len(remote_payload_paths),
        "uploaded": sorted(uploaded, key=lambda item: item["path"]),
        "deleted": sorted(deleted),
        "unchanged": len(snapshots) - len(diff.upload_paths),
        "signatures_written": wrote_signatures,
        "success": True,
    }


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source_dir", type=Path, help="Local Git working tree root.")
    parser.add_argument(
        "--target-folder",
        required=True,
        help="Lakehouse Files target folder, for example Files/DWG-platform/weaver.",
    )
    workspace = parser.add_mutually_exclusive_group(required=False)
    workspace.add_argument("--workspace-id", default=os.environ.get("FABRIC_WORKSPACE_ID"))
    workspace.add_argument(
        "--workspace-name",
        default=os.environ.get("FABRIC_WORKSPACE_NAME", DEFAULT_WORKSPACE_NAME),
    )
    lakehouse = parser.add_mutually_exclusive_group(required=False)
    lakehouse.add_argument("--lakehouse-id", default=os.environ.get("FABRIC_LAKEHOUSE_ID"))
    lakehouse.add_argument(
        "--lakehouse-name",
        default=os.environ.get("FABRIC_LAKEHOUSE_NAME", DEFAULT_LAKEHOUSE_NAME),
    )
    parser.add_argument("--api-base-url", default=os.environ.get("FABRIC_API_BASE_URL", DEFAULT_API_BASE_URL))
    parser.add_argument("--onelake-base-url", default=os.environ.get("ONELAKE_BASE_URL", DEFAULT_ONELAKE_BASE_URL))
    parser.add_argument("--fabric-scope", default=os.environ.get("FABRIC_API_SCOPE", DEFAULT_FABRIC_SCOPE))
    parser.add_argument("--storage-scope", default=os.environ.get("ONELAKE_SCOPE", DEFAULT_STORAGE_SCOPE))
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Calculate local signatures only; do not call Fabric or OneLake.",
    )
    parser.add_argument(
        "--show-signatures",
        action="store_true",
        help="Include full local signatures in --dry-run JSON output.",
    )
    return parser.parse_args()


def main() -> int:
    """CLI entrypoint."""

    args = parse_args()
    if args.dry_run:
        result = sync_repository(
            source_dir=args.source_dir,
            target_folder=args.target_folder,
            workspace_id="dry-run",
            lakehouse_id="dry-run",
            storage_token="dry-run",
            onelake_base_url=args.onelake_base_url,
            workers=args.workers,
            dry_run=True,
            include_signatures=args.show_signatures,
        )
    else:
        if not args.workspace_id and not args.workspace_name:
            raise GitRepoSyncError(
                "provide --workspace-id or --workspace-name "
                "(or set FABRIC_WORKSPACE_ID or FABRIC_WORKSPACE_NAME)"
            )
        workspace_id, workspace_name, lakehouse, storage_token = resolve_fabric_target(args)
        result = sync_repository(
            source_dir=args.source_dir,
            target_folder=args.target_folder,
            workspace_id=workspace_id,
            lakehouse_id=str(lakehouse["id"]),
            storage_token=storage_token,
            onelake_base_url=args.onelake_base_url,
            workers=args.workers,
            dry_run=False,
        )
        result["workspace_id"] = workspace_id
        result["workspace_name"] = workspace_name
        result["lakehouse"] = lakehouse

    print(json.dumps(result, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (GitRepoSyncError, SyncError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
