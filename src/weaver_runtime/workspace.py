from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from azure.identity import DefaultAzureCredential

from ._legacy import load_script_module
from .errors import CommandError


SUPPORTED_WORKSPACE_SUFFIXES = {".ipynb", ".py"}


@dataclass(frozen=True)
class WorkspaceItemSource:
    name: str
    path: Path


@dataclass(frozen=True)
class WorkspacePushRequest:
    source: Path
    workspace_name: str | None
    workspace_id: str | None
    item: str | None
    description: str | None
    prune: bool
    update_metadata: bool
    dry_run: bool
    api_base_url: str
    scope: str


def discover_workspace_sources(
    source_dir: Path,
    *,
    item_name: str | None = None,
) -> list[WorkspaceItemSource]:
    if not source_dir.exists():
        raise CommandError(f"workspace source not found: {source_dir}")
    if not source_dir.is_dir():
        raise CommandError(f"workspace source is not a directory: {source_dir}")

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
            raise CommandError(f"workspace item not found in source: {item_name!r}")
    return items


def push_workspace(request: WorkspacePushRequest) -> dict[str, Any]:
    """Create or update Fabric Notebook items from an explicit request."""
    deploy_notebook = load_script_module("deploy_fabric_notebook")
    source_dir = request.source
    items = discover_workspace_sources(source_dir, item_name=request.item)

    if request.dry_run:
        return {
            "source": str(source_dir),
            "workspace_id": request.workspace_id,
            "workspace_name": request.workspace_name,
            "items": [
                {"name": item.name, "source_path": str(item.path), "action": "would_push"}
                for item in items
            ],
            "dry_run": True,
            "success": True,
        }

    if request.prune:
        raise CommandError("--prune is not implemented yet; default push never deletes remote items")

    token = DefaultAzureCredential().get_token(request.scope).token
    workspace_id = request.workspace_id or deploy_notebook.resolve_workspace_id(
        request.api_base_url,
        token,
        request.workspace_name,
    )
    results = []
    for item in items:
        notebook_id = deploy_notebook.resolve_notebook_id(
            request.api_base_url,
            token,
            workspace_id,
            item.name,
        )
        if notebook_id:
            results.append(
                deploy_notebook.update_notebook_definition(
                    api_base_url=request.api_base_url,
                    token=token,
                    workspace_id=workspace_id,
                    notebook_id=notebook_id,
                    notebook_name=item.name,
                    source_path=item.path,
                    update_metadata=request.update_metadata,
                )
            )
        else:
            results.append(
                deploy_notebook.create_notebook(
                    api_base_url=request.api_base_url,
                    token=token,
                    workspace_id=workspace_id,
                    notebook_name=item.name,
                    source_path=item.path,
                    description=request.description,
                )
            )

    return {
        "source": str(source_dir),
        "workspace_id": workspace_id,
        "workspace_name": request.workspace_name,
        "items": results,
        "success": True,
    }


def print_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2), flush=True)
