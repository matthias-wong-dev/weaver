"""Resolve a concrete OneLake target from workspace/lakehouse names or ids."""

from __future__ import annotations

from . import auth, resources
from .onelake import LakehouseTarget
from .settings import FabricSettings


def resolve_lakehouse_target(
    settings: FabricSettings,
    *,
    workspace_id: str | None = None,
    workspace_name: str | None = None,
    lakehouse_id: str | None = None,
    lakehouse_name: str | None = None,
) -> LakehouseTarget:
    """Resolve workspace/lakehouse ids and acquire a OneLake storage token."""

    credential = auth.credential()
    fabric_token = auth.get_token(settings.fabric_scope, credential)
    resolved_workspace_id = workspace_id or resources.find_workspace_id(
        fabric_token, workspace_name, settings.api_base_url
    )
    display_name = resources.workspace_display_name(
        fabric_token, resolved_workspace_id, settings.api_base_url
    )
    lakehouse = resources.resolve_lakehouse(
        token=fabric_token,
        workspace_id=resolved_workspace_id,
        lakehouse_id=lakehouse_id,
        lakehouse_name=lakehouse_name,
        api_base_url=settings.api_base_url,
    )
    storage_token = auth.get_token(settings.storage_scope, credential)
    return LakehouseTarget(
        workspace_id=resolved_workspace_id,
        lakehouse_id=str(lakehouse["id"]),
        storage_token=storage_token,
        onelake_base_url=settings.onelake_base_url,
        workspace_name=display_name,
        lakehouse_name=lakehouse.get("displayName"),
    )
