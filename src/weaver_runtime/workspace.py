from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from azure.identity import DefaultAzureCredential

from ._legacy import load_script_module
from .config import WeaverConfig, WeaverConfigError


SUPPORTED_WORKSPACE_SUFFIXES = {".ipynb", ".py"}


@dataclass(frozen=True)
class WorkspaceItemSource:
    name: str
    path: Path


def discover_workspace_sources(
    source_dir: Path,
    *,
    item_name: str | None = None,
) -> list[WorkspaceItemSource]:
    if not source_dir.exists():
        raise WeaverConfigError(f"workspace source not found: {source_dir}")
    if not source_dir.is_dir():
        raise WeaverConfigError(f"workspace source is not a directory: {source_dir}")

    items = [
        WorkspaceItemSource(name=path.stem, path=path)
        for path in sorted(source_dir.iterdir())
        if path.is_file()
        and not path.name.startswith(".")
        and path.suffix.lower() in SUPPORTED_WORKSPACE_SUFFIXES
    ]
    if item_name:
        items = [item for item in items if item.name == item_name]
        if not items:
            raise WeaverConfigError(f"workspace item not found in source: {item_name!r}")
    return items


def push_workspace(config: WeaverConfig, args: argparse.Namespace) -> dict[str, Any]:
    """Create or update configured Fabric Notebook items from local workspace files."""

    if config.fabric.workspace is None:
        raise WeaverConfigError("fabric.workspace is required")

    deploy_notebook = load_script_module("deploy_fabric_notebook")
    source_dir = args.source or config.fabric.workspace.source
    if source_dir is None:
        raise WeaverConfigError("provide --source or configure fabric.workspace.source")

    items = discover_workspace_sources(source_dir, item_name=args.item)
    workspace_name = args.workspace_name or config.fabric.workspace.name

    if args.dry_run:
        return {
            "config": str(config.path),
            "source": str(source_dir),
            "workspace_id": args.workspace_id,
            "workspace_name": workspace_name if not args.workspace_id else None,
            "items": [
                {"name": item.name, "source_path": str(item.path), "action": "would_push"}
                for item in items
            ],
            "dry_run": True,
            "success": True,
        }

    if args.prune:
        raise WeaverConfigError("--prune is not implemented yet; default push never deletes remote items")
    if not args.workspace_id and not workspace_name:
        raise WeaverConfigError("provide --workspace-id or configure fabric.workspace.name")

    token = DefaultAzureCredential().get_token(args.scope).token
    workspace_id = args.workspace_id or deploy_notebook.resolve_workspace_id(
        args.api_base_url,
        token,
        workspace_name,
    )
    results = []
    for item in items:
        notebook_id = deploy_notebook.resolve_notebook_id(
            args.api_base_url,
            token,
            workspace_id,
            item.name,
        )
        if notebook_id:
            results.append(
                deploy_notebook.update_notebook_definition(
                    api_base_url=args.api_base_url,
                    token=token,
                    workspace_id=workspace_id,
                    notebook_id=notebook_id,
                    notebook_name=item.name,
                    source_path=item.path,
                    update_metadata=args.update_metadata,
                )
            )
        else:
            results.append(
                deploy_notebook.create_notebook(
                    api_base_url=args.api_base_url,
                    token=token,
                    workspace_id=workspace_id,
                    notebook_name=item.name,
                    source_path=item.path,
                    description=args.description,
                )
            )

    return {
        "config": str(config.path),
        "source": str(source_dir),
        "workspace_id": workspace_id,
        "workspace_name": workspace_name if not args.workspace_id else None,
        "items": results,
        "success": True,
    }


def print_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2), flush=True)
