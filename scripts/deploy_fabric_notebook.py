#!/usr/bin/env python3
"""Create or update a Microsoft Fabric Notebook item from a local notebook source file.

By default, the local source filename stem becomes the Fabric notebook display name:

    notebooks/export.ipynb  ->  export
    notebooks/load_mfs.py   ->  load_mfs

Override the target display name with --notebook-name when needed.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from azure.identity import DefaultAzureCredential


API_BASE_URL = "https://api.fabric.microsoft.com"
FABRIC_SCOPE = "https://analysis.windows.net/powerbi/api/.default"
DEFAULT_WORKSPACE_NAME = None
SUPPORTED_SUFFIXES = {".ipynb", ".py"}


class FabricDeployError(RuntimeError):
    """Raised when a Fabric API request or deployment operation fails."""


def request(
    method: str,
    url: str,
    token: str,
    payload: dict[str, Any] | None = None,
    expected_statuses: set[int] | None = None,
) -> tuple[dict[str, Any], dict[str, str], int]:
    """Call a Fabric JSON REST endpoint."""

    body = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"

    req = Request(url, data=body, headers=headers, method=method)

    try:
        with urlopen(req, timeout=120) as response:
            response_body = response.read().decode("utf-8")
            if expected_statuses and response.status not in expected_statuses:
                raise FabricDeployError(
                    f"{method} {url} returned HTTP {response.status}: {response_body}"
                )

            return (
                json.loads(response_body) if response_body else {},
                {key.lower(): value for key, value in response.headers.items()},
                response.status,
            )

    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise FabricDeployError(f"{method} {url} returned HTTP {exc.code}: {detail}") from exc
    except URLError as exc:
        raise FabricDeployError(f"{method} {url} failed: {exc.reason}") from exc


def paged_values(url: str, token: str) -> list[dict[str, Any]]:
    """Return all values from a paged Fabric endpoint."""

    values: list[dict[str, Any]] = []

    while url:
        payload, _, _ = request("GET", url, token, expected_statuses={200})
        values.extend(payload.get("value", []))
        url = payload.get("continuationUri") or payload.get("nextLink") or ""

    return values


def fabric_url(api_base_url: str, path: str) -> str:
    """Return an absolute Fabric API URL."""

    return f"{api_base_url.rstrip('/')}/v1/{path.lstrip('/')}"


def resolve_workspace_id(api_base_url: str, token: str, workspace_name: str) -> str:
    """Resolve one Fabric workspace ID by display name."""

    workspaces = paged_values(fabric_url(api_base_url, "workspaces"), token)
    matches = [
        workspace
        for workspace in workspaces
        if workspace.get("displayName") == workspace_name
    ]

    if len(matches) != 1:
        available = sorted(str(workspace.get("displayName")) for workspace in workspaces)
        raise FabricDeployError(
            f"expected one workspace named {workspace_name!r}, found {len(matches)}; "
            f"available workspaces: {available}"
        )

    return str(matches[0]["id"])


def resolve_notebook_id(
    api_base_url: str,
    token: str,
    workspace_id: str,
    notebook_name: str,
) -> str | None:
    """Resolve a Fabric Notebook item ID by display name, or return None when missing."""

    items_url = fabric_url(api_base_url, f"workspaces/{quote(workspace_id)}/items")
    items = paged_values(items_url, token)

    matches = [
        item
        for item in items
        if item.get("type") == "Notebook" and item.get("displayName") == notebook_name
    ]

    if len(matches) > 1:
        raise FabricDeployError(
            f"expected zero or one notebook named {notebook_name!r}, found {len(matches)}"
        )

    return str(matches[0]["id"]) if matches else None


def default_notebook_name(source_path: Path) -> str:
    """Return the default Fabric target name for a local notebook source file."""

    return source_path.stem


def platform_definition_part(notebook_name: str, description: str | None = None) -> dict[str, str]:
    """Build the Fabric .platform definition part required for metadata updates."""

    payload = {
        "$schema": (
            "https://developer.microsoft.com/json-schemas/fabric/gitIntegration/"
            "platformProperties/2.0.0/schema.json"
        ),
        "metadata": {
            "type": "Notebook",
            "displayName": notebook_name,
            "description": description or "New notebook",
        },
        "config": {
            "version": "2.0",
            "logicalId": "00000000-0000-0000-0000-000000000000",
        },
    }
    return {
        "path": ".platform",
        "payload": base64.b64encode(
            json.dumps(payload, indent=2).encode("utf-8")
        ).decode("utf-8"),
        "payloadType": "InlineBase64",
    }


def notebook_definition_payload(
    source_path: Path,
    notebook_name: str | None = None,
    description: str | None = None,
) -> dict[str, Any]:
    """Build a Fabric notebook definition from a local .ipynb or .py source file."""

    suffix = source_path.suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        raise FabricDeployError(
            f"unsupported notebook source suffix {source_path.suffix!r}; "
            f"supported suffixes: {sorted(SUPPORTED_SUFFIXES)}"
        )

    if suffix == ".ipynb":
        definition_format = "ipynb"
        part_path = "notebook-content.ipynb"
    else:
        definition_format = "fabricGitSource"
        part_path = "notebook-content.py"

    content = source_path.read_bytes()

    parts = [
        {
            "path": part_path,
            "payload": base64.b64encode(content).decode("utf-8"),
            "payloadType": "InlineBase64",
        }
    ]
    if notebook_name is not None:
        parts.append(platform_definition_part(notebook_name, description))

    return {
        "definition": {
            "format": definition_format,
            "parts": parts,
        }
    }


def create_notebook(
    api_base_url: str,
    token: str,
    workspace_id: str,
    notebook_name: str,
    source_path: Path,
    description: str | None = None,
) -> dict[str, Any]:
    """Create a Fabric Notebook item from a local source file."""

    url = fabric_url(api_base_url, f"workspaces/{quote(workspace_id)}/notebooks")
    payload: dict[str, Any] = {
        "displayName": notebook_name,
        **notebook_definition_payload(source_path),
    }

    if description:
        payload["description"] = description

    response, headers, status = request(
        "POST",
        url,
        token,
        payload=payload,
        expected_statuses={200, 201, 202},
    )
    response = response or {}

    notebook_id = response.get("id") or response.get("itemId")
    if not notebook_id:
        notebook_id = resolve_notebook_id(api_base_url, token, workspace_id, notebook_name)

    return {
        "action": "created",
        "workspace_id": workspace_id,
        "notebook_id": notebook_id,
        "notebook_name": notebook_name,
        "source_path": str(source_path),
        "status_code": status,
        "operation": headers.get("location"),
        "response": response,
        "success": True,
    }


def update_notebook_definition(
    api_base_url: str,
    token: str,
    workspace_id: str,
    notebook_id: str,
    notebook_name: str,
    source_path: Path,
    update_metadata: bool,
) -> dict[str, Any]:
    """Update an existing Fabric Notebook item definition from a local source file."""

    query = "?updateMetadata=True" if update_metadata else ""
    url = fabric_url(
        api_base_url,
        f"workspaces/{quote(workspace_id)}/notebooks/{quote(notebook_id)}/updateDefinition{query}",
    )
    payload = notebook_definition_payload(
        source_path,
        notebook_name=notebook_name if update_metadata else None,
    )

    response, headers, status = request(
        "POST",
        url,
        token,
        payload=payload,
        expected_statuses={200, 202},
    )
    response = response or {}

    return {
        "action": "updated",
        "workspace_id": workspace_id,
        "notebook_id": notebook_id,
        "notebook_name": notebook_name,
        "source_path": str(source_path),
        "status_code": status,
        "operation": headers.get("location"),
        "response": response,
        "success": True,
    }


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "source_path",
        type=Path,
        help="Local notebook source file. The filename stem is the default Fabric notebook name.",
    )
    parser.add_argument(
        "--notebook-name",
        help="Fabric Notebook display name. Defaults to the source filename stem.",
    )
    parser.add_argument(
        "--notebook-id",
        help="Existing Fabric Notebook item ID. If supplied, the script updates this item directly.",
    )

    workspace = parser.add_mutually_exclusive_group(required=False)
    workspace.add_argument("--workspace-id", default=os.environ.get("FABRIC_WORKSPACE_ID"))
    workspace.add_argument(
        "--workspace-name",
        default=os.environ.get("FABRIC_WORKSPACE_NAME", DEFAULT_WORKSPACE_NAME),
    )

    parser.add_argument("--description")
    parser.add_argument("--api-base-url", default=os.environ.get("FABRIC_API_BASE_URL", API_BASE_URL))
    parser.add_argument("--scope", default=os.environ.get("FABRIC_API_SCOPE", FABRIC_SCOPE))
    parser.add_argument(
        "--no-create",
        action="store_true",
        help="Fail if the target notebook does not exist.",
    )
    parser.add_argument(
        "--update-metadata",
        action="store_true",
        help=(
            "Also update Fabric item metadata from a .platform part. "
            "Do not use this for plain .ipynb/.py notebook source files."
        ),
    )
    parser.add_argument("--dry-run", action="store_true")

    return parser.parse_args()


def main() -> int:
    """CLI entrypoint."""

    args = parse_args()
    source_path = args.source_path

    if not source_path.exists():
        raise FabricDeployError(f"local notebook source not found: {source_path}")
    if not source_path.is_file():
        raise FabricDeployError(f"local notebook source is not a file: {source_path}")

    notebook_name = args.notebook_name or default_notebook_name(source_path)

    if args.dry_run:
        print(
            json.dumps(
                {
                    "source_path": str(source_path),
                    "defaulted_notebook_name": args.notebook_name is None,
                    "notebook_name": notebook_name,
                    "notebook_id": args.notebook_id,
                    "workspace_id": args.workspace_id,
                    "workspace_name": args.workspace_name if not args.workspace_id else None,
                    "would_create_if_missing": not args.no_create,
                    "update_metadata": args.update_metadata,
                    "success": True,
                },
                indent=2,
            ),
            flush=True,
        )
        return 0

    if not args.workspace_id and not args.workspace_name:
        raise FabricDeployError(
            "provide --workspace-id or --workspace-name "
            "(or set FABRIC_WORKSPACE_ID or FABRIC_WORKSPACE_NAME)"
        )

    token = DefaultAzureCredential().get_token(args.scope).token

    workspace_id = args.workspace_id or resolve_workspace_id(
        args.api_base_url,
        token,
        args.workspace_name,
    )

    notebook_id = args.notebook_id or resolve_notebook_id(
        args.api_base_url,
        token,
        workspace_id,
        notebook_name,
    )

    if notebook_id:
        result = update_notebook_definition(
            api_base_url=args.api_base_url,
            token=token,
            workspace_id=workspace_id,
            notebook_id=notebook_id,
            notebook_name=notebook_name,
            source_path=source_path,
            update_metadata=args.update_metadata,
        )
    else:
        if args.no_create:
            raise FabricDeployError(
                f"notebook not found and --no-create was supplied: {notebook_name!r}"
            )

        result = create_notebook(
            api_base_url=args.api_base_url,
            token=token,
            workspace_id=workspace_id,
            notebook_name=notebook_name,
            source_path=source_path,
            description=args.description,
        )

    print(json.dumps(result, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except FabricDeployError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
