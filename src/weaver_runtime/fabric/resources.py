"""Resolve Fabric workspaces, lakehouses, and workspace items by name or id."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from .client import FabricClientError, fabric_url, paged_values, request_json
from .settings import DEFAULT_API_BASE_URL


class ResourceError(FabricClientError):
    """Raised when a workspace, lakehouse, or item cannot be resolved."""


def list_items(
    token: str,
    workspace_id: str,
    api_base_url: str = DEFAULT_API_BASE_URL,
) -> list[dict[str, Any]]:
    """Return all items in a Fabric workspace."""

    return paged_values(
        fabric_url(api_base_url, f"workspaces/{quote(workspace_id)}/items"), token
    )


def find_workspace_id(
    token: str,
    workspace_name: str,
    api_base_url: str = DEFAULT_API_BASE_URL,
) -> str:
    """Find a Fabric workspace id by display name."""

    matches = [
        workspace
        for workspace in paged_values(fabric_url(api_base_url, "workspaces"), token)
        if workspace.get("displayName") == workspace_name
    ]
    if len(matches) == 1:
        return str(matches[0]["id"])
    if not matches:
        raise ResourceError(f"workspace not found: {workspace_name!r}")
    raise ResourceError(f"multiple workspaces named {workspace_name!r}")


def workspace_display_name(
    token: str,
    workspace_id: str,
    api_base_url: str = DEFAULT_API_BASE_URL,
) -> str | None:
    """Return the workspace display name for one workspace id."""

    payload, _, _ = request_json(
        "GET", fabric_url(api_base_url, f"workspaces/{quote(workspace_id)}"), token
    )
    return (payload or {}).get("displayName")


def resolve_lakehouse(
    token: str,
    workspace_id: str,
    lakehouse_id: str | None = None,
    lakehouse_name: str | None = None,
    api_base_url: str = DEFAULT_API_BASE_URL,
) -> dict[str, Any]:
    """Resolve the Lakehouse item to use, by id, name, or sole-lakehouse."""

    lakehouses = [
        item
        for item in list_items(token, workspace_id, api_base_url)
        if item.get("type") == "Lakehouse"
    ]

    if lakehouse_id:
        for lakehouse in lakehouses:
            if lakehouse.get("id") == lakehouse_id:
                return lakehouse
        raise ResourceError(f"lakehouse id not found: {lakehouse_id!r}")

    if lakehouse_name:
        for lakehouse in lakehouses:
            if lakehouse.get("displayName") == lakehouse_name:
                return lakehouse
        raise ResourceError(f"lakehouse name not found: {lakehouse_name!r}")

    if len(lakehouses) == 1:
        return lakehouses[0]

    names = [lakehouse.get("displayName") for lakehouse in lakehouses]
    raise ResourceError(f"provide --lakehouse-id or --lakehouse-name; found lakehouses: {names}")


def resolve_item_id(
    token: str,
    workspace_id: str,
    item_name: str,
    item_type: str,
    api_base_url: str = DEFAULT_API_BASE_URL,
) -> str | None:
    """Return the id of one workspace item by display name and type, if present."""

    for item in list_items(token, workspace_id, api_base_url):
        if item.get("type") == item_type and item.get("displayName") == item_name:
            return str(item["id"])
    return None
