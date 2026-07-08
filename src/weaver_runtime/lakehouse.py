from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ._legacy import load_script_module
from .config import LakehouseConfig, WeaverConfig, WeaverConfigError


@dataclass(frozen=True)
class LakehouseSyncRepository:
    name: str
    source: Path
    target_folder: str


def configured_repositories(
    lakehouse: LakehouseConfig,
    *,
    selected: list[str] | None = None,
    target_root: str | None = None,
) -> list[LakehouseSyncRepository]:
    sync_git_repo = load_script_module("sync_git_repo")
    selected_set = set(selected or [])
    root = target_root or lakehouse.target_root
    repositories = [
        LakehouseSyncRepository(
            name=repository.name,
            source=repository.source,
            target_folder=sync_git_repo.normalise_files_folder(
                f"{root}/{repository.target}"
            ),
        )
        for repository in lakehouse.repositories
        if not selected_set or repository.name in selected_set
    ]
    missing = selected_set - {repository.name for repository in repositories}
    if missing:
        raise WeaverConfigError(f"unknown configured repositories: {sorted(missing)}")
    if not repositories:
        raise WeaverConfigError("no repositories selected")
    return repositories


def sync_lakehouse(config: WeaverConfig, args: argparse.Namespace) -> dict[str, Any]:
    """Synchronise configured Git repositories to a configured Lakehouse."""

    if config.fabric.lakehouse is None:
        raise WeaverConfigError("fabric.lakehouse is required")

    sync_folder = load_script_module("sync_folder")
    sync_git_repo = load_script_module("sync_git_repo")
    lakehouse = config.fabric.lakehouse
    repositories = configured_repositories(
        lakehouse,
        selected=args.repository,
        target_root=getattr(args, "target_root", None),
    )
    workspace_name = args.workspace_name or lakehouse.workspace or (
        config.fabric.workspace.name if config.fabric.workspace else None
    )
    lakehouse_name = args.lakehouse_name or lakehouse.name
    target_args = argparse.Namespace(
        workspace_id=args.workspace_id,
        workspace_name=workspace_name,
        lakehouse_id=args.lakehouse_id,
        lakehouse_name=lakehouse_name,
        api_base_url=args.api_base_url,
        fabric_scope=args.fabric_scope,
        storage_scope=args.storage_scope,
    )

    if args.dry_run:
        workspace_id = "dry-run"
        workspace_display_name = workspace_name or "dry-run"
        lakehouse_id = "dry-run"
        storage_token = "dry-run"
        lakehouse_payload: dict[str, Any] = {
            "id": lakehouse_id,
            "displayName": lakehouse_name or "dry-run",
        }
    else:
        if not args.workspace_id and not workspace_name:
            raise WeaverConfigError("provide --workspace-id or configure fabric.lakehouse.workspace")
        if not args.lakehouse_id and not lakehouse_name:
            raise WeaverConfigError("provide --lakehouse-id or configure fabric.lakehouse.name")
        workspace_id, workspace_display_name, lakehouse_payload, storage_token = (
            sync_git_repo.resolve_fabric_target(target_args)
        )
        lakehouse_id = str(lakehouse_payload["id"])

    results = [
        _sync_one(
            repository,
            sync_git_repo,
            workspace_id=workspace_id,
            lakehouse_id=lakehouse_id,
            storage_token=storage_token,
            onelake_base_url=args.onelake_base_url,
            workers=args.workers,
            dry_run=args.dry_run,
            show_signatures=args.show_signatures,
        )
        for repository in repositories
    ]
    return {
        "config": str(config.path),
        "workspace_id": workspace_id,
        "workspace_name": workspace_display_name,
        "lakehouse": lakehouse_payload,
        "results": results,
        "success": True,
        "api_base_url": args.api_base_url or sync_folder.DEFAULT_API_BASE_URL,
    }


def _sync_one(
    repository: LakehouseSyncRepository,
    sync_git_repo,
    *,
    workspace_id: str,
    lakehouse_id: str,
    storage_token: str,
    onelake_base_url: str,
    workers: int,
    dry_run: bool,
    show_signatures: bool,
) -> dict[str, Any]:
    result = sync_git_repo.sync_repository(
        source_dir=repository.source,
        target_folder=repository.target_folder,
        workspace_id=workspace_id,
        lakehouse_id=lakehouse_id,
        storage_token=storage_token,
        onelake_base_url=onelake_base_url,
        workers=workers,
        dry_run=dry_run,
        include_signatures=show_signatures,
    )
    result["repository"] = repository.name
    return result


def print_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2), flush=True)
