#!/usr/bin/env python3
"""Synchronise the configured DWG platform Git repositories into Fabric."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from sync_folder import (
    DEFAULT_API_BASE_URL,
    DEFAULT_FABRIC_SCOPE,
    DEFAULT_LAKEHOUSE_NAME,
    DEFAULT_ONELAKE_BASE_URL,
    DEFAULT_STORAGE_SCOPE,
    DEFAULT_WORKSPACE_NAME,
    SyncError,
)
from sync_git_repo import (
    DEFAULT_WORKERS,
    GitRepoSyncError,
    git_visible_files,
    normalise_files_folder,
    resolve_fabric_target,
    sync_repository,
)


class PlatformSyncError(RuntimeError):
    """Raised when platform sync configuration or execution fails."""


@dataclass(frozen=True)
class RepositoryConfig:
    """One repository mirror mapping."""

    source: Path
    target_folder: str
    name: str


def load_sync_config(config_path: Path) -> dict[str, Any]:
    """Load and validate the platform sync YAML."""

    if not config_path.exists():
        raise PlatformSyncError(f"sync config not found: {config_path}")
    payload = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise PlatformSyncError("sync config must be a YAML object")
    if payload.get("version") != 1:
        raise PlatformSyncError(f"unsupported sync config version: {payload.get('version')!r}")
    repositories = payload.get("repositories")
    if not isinstance(repositories, list) or not repositories:
        raise PlatformSyncError("sync config repositories must be a non-empty list")
    target_root = payload.get("target_root")
    if not isinstance(target_root, str) or not target_root.strip():
        raise PlatformSyncError("sync config target_root must be a non-empty string")
    return payload


def infer_platform_root(config_path: Path, repository_entries: list[dict[str, Any]]) -> Path:
    """Infer the containing dwg-platform directory for repository source paths."""

    candidates = [config_path.parent.parent, config_path.parent]
    for candidate in candidates:
        if all((candidate / str(entry.get("source", ""))).exists() for entry in repository_entries):
            return candidate.resolve()
    sources = [str(entry.get("source", "")) for entry in repository_entries]
    raise PlatformSyncError(
        f"could not infer platform root for configured sources {sources} from {config_path}"
    )


def repository_configs(config_path: Path) -> tuple[Path, list[RepositoryConfig]]:
    """Return resolved repository sync mappings."""

    config_path = config_path.resolve()
    payload = load_sync_config(config_path)
    raw_repositories = payload["repositories"]
    for entry in raw_repositories:
        if not isinstance(entry, dict):
            raise PlatformSyncError("repository entries must be YAML objects")

    platform_root = infer_platform_root(config_path, raw_repositories)
    target_root = normalise_files_folder(str(payload["target_root"]))
    repositories: list[RepositoryConfig] = []

    for entry in raw_repositories:
        source_name = entry.get("source")
        target_name = entry.get("target")
        if not isinstance(source_name, str) or not source_name.strip():
            raise PlatformSyncError("repository source must be a non-empty string")
        if not isinstance(target_name, str) or not target_name.strip():
            raise PlatformSyncError("repository target must be a non-empty string")
        source = (platform_root / source_name).resolve()
        if not source.exists():
            raise PlatformSyncError(f"repository source not found: {source}")
        target_folder = normalise_files_folder(f"{target_root}/{target_name}")
        repositories.append(
            RepositoryConfig(
                source=source,
                target_folder=target_folder,
                name=target_name,
            )
        )

    return platform_root, repositories


def quick_fingerprint(repository: RepositoryConfig) -> tuple[tuple[str, int, int], ...]:
    """Return a cheap fingerprint for watch-mode change detection."""

    values: list[tuple[str, int, int]] = []
    for relative_path in git_visible_files(repository.source):
        path = repository.source / Path(relative_path)
        if not path.is_file():
            continue
        stat = path.stat()
        values.append((relative_path, stat.st_size, stat.st_mtime_ns))
    return tuple(values)


def sync_one(
    repository: RepositoryConfig,
    args: argparse.Namespace,
    workspace_id: str,
    lakehouse_id: str,
    storage_token: str,
) -> dict[str, Any]:
    """Synchronise one configured repository."""

    print(
        f"sync {repository.name}: {repository.source} -> Files/{repository.target_folder}",
        file=sys.stderr,
        flush=True,
    )
    result = sync_repository(
        source_dir=repository.source,
        target_folder=repository.target_folder,
        workspace_id=workspace_id,
        lakehouse_id=lakehouse_id,
        storage_token=storage_token,
        onelake_base_url=args.onelake_base_url,
        workers=args.workers,
        dry_run=args.dry_run,
        include_signatures=args.show_signatures,
    )
    result["repository"] = repository.name
    return result


def sync_all(
    repositories: list[RepositoryConfig],
    args: argparse.Namespace,
    workspace_id: str,
    lakehouse_id: str,
    storage_token: str,
) -> list[dict[str, Any]]:
    """Synchronise all configured repositories."""

    return [
        sync_one(repository, args, workspace_id, lakehouse_id, storage_token)
        for repository in repositories
    ]


def watch_repositories(
    repositories: list[RepositoryConfig],
    args: argparse.Namespace,
    workspace_id: str,
    lakehouse_id: str,
    storage_token: str,
) -> None:
    """Poll configured repositories and reconcile changed repos after a debounce."""

    print("initial reconciliation", file=sys.stderr, flush=True)
    initial = sync_all(repositories, args, workspace_id, lakehouse_id, storage_token)
    print(json.dumps({"results": initial}, indent=2), flush=True)

    fingerprints = {repository.name: quick_fingerprint(repository) for repository in repositories}
    pending: dict[str, float] = {}
    by_name = {repository.name: repository for repository in repositories}
    next_full = time.monotonic() + args.full_interval

    while True:
        now = time.monotonic()
        for repository in repositories:
            current = quick_fingerprint(repository)
            if current != fingerprints[repository.name]:
                fingerprints[repository.name] = current
                pending[repository.name] = now + args.debounce

        ready = sorted(name for name, due in pending.items() if due <= now)
        for name in ready:
            repository = by_name[name]
            result = sync_one(repository, args, workspace_id, lakehouse_id, storage_token)
            print(json.dumps({"results": [result]}, indent=2), flush=True)
            fingerprints[name] = quick_fingerprint(repository)
            pending.pop(name, None)

        if now >= next_full:
            print("periodic full reconciliation", file=sys.stderr, flush=True)
            results = sync_all(repositories, args, workspace_id, lakehouse_id, storage_token)
            print(json.dumps({"results": results}, indent=2), flush=True)
            fingerprints = {
                repository.name: quick_fingerprint(repository)
                for repository in repositories
            }
            pending.clear()
            next_full = now + args.full_interval

        time.sleep(args.watch_interval)


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--watch-interval", type=float, default=2.0)
    parser.add_argument("--debounce", type=float, default=1.0)
    parser.add_argument("--full-interval", type=float, default=300.0)
    parser.add_argument("--repository", action="append", default=[], help="Limit sync to one target name. Repeatable.")
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
    platform_root, repositories = repository_configs(args.config)
    if args.repository:
        selected = set(args.repository)
        repositories = [repository for repository in repositories if repository.name in selected]
        missing = selected - {repository.name for repository in repositories}
        if missing:
            raise PlatformSyncError(f"unknown configured repositories: {sorted(missing)}")
    if not repositories:
        raise PlatformSyncError("no repositories selected")

    if args.dry_run:
        workspace_id = "dry-run"
        lakehouse_id = "dry-run"
        storage_token = "dry-run"
        lakehouse: dict[str, Any] = {"id": lakehouse_id, "displayName": "dry-run"}
        workspace_name = "dry-run"
    else:
        if not args.workspace_id and not args.workspace_name:
            raise PlatformSyncError(
                "provide --workspace-id or --workspace-name "
                "(or set FABRIC_WORKSPACE_ID or FABRIC_WORKSPACE_NAME)"
            )
        workspace_id, workspace_name, lakehouse, storage_token = resolve_fabric_target(args)
        lakehouse_id = str(lakehouse["id"])

    if args.watch:
        watch_repositories(repositories, args, workspace_id, lakehouse_id, storage_token)
        return 0

    results = sync_all(repositories, args, workspace_id, lakehouse_id, storage_token)
    print(
        json.dumps(
            {
                "platform_root": str(platform_root),
                "workspace_id": workspace_id,
                "workspace_name": workspace_name,
                "lakehouse": lakehouse,
                "results": results,
                "success": True,
            },
            indent=2,
        ),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (PlatformSyncError, GitRepoSyncError, SyncError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
