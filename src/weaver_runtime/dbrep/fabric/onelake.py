"""OneLake resolution + upload for the Fabric Lakehouse dbrep backend.

Thin bridge over the shared :mod:`weaver_runtime.fabric` substrate. Runtime file
movement goes through the shared folder-sync layer (signature diff, scoped
delete); ``resolve_lakehouse`` returns a plain dict for backward compatibility.
"""

from __future__ import annotations

from pathlib import Path

from ..errors import BuildError


def resolve_lakehouse(workspace: str, lakehouse: str) -> dict:
    """Resolve workspace/lakehouse ids and a OneLake storage token."""

    from ...fabric.context import resolve_lakehouse_target
    from ...fabric.settings import resolve_settings

    target = resolve_lakehouse_target(
        resolve_settings(),
        workspace_name=workspace,
        lakehouse_name=lakehouse,
    )
    return {
        "workspace_id": target.workspace_id,
        "workspace_name": target.workspace_name,
        "lakehouse_id": target.lakehouse_id,
        "lakehouse_name": target.lakehouse_name,
        "storage_token": target.storage_token,
        "onelake_base_url": target.onelake_base_url,
    }


def _target(resolved: dict):
    from ...fabric.onelake import LakehouseTarget

    return LakehouseTarget(
        workspace_id=resolved["workspace_id"],
        lakehouse_id=resolved["lakehouse_id"],
        storage_token=resolved["storage_token"],
        onelake_base_url=resolved["onelake_base_url"],
        workspace_name=resolved.get("workspace_name"),
        lakehouse_name=resolved.get("lakehouse_name"),
    )


def sync_runtime_folder(
    files_root: Path, resolved: dict, *, degrees_of_parallelism: int = 32
) -> int:
    """Sync a staged ``Files/`` bundle tree to OneLake via the shared layer.

    ``Files/_weaver/runtime`` is Weaver-owned, so it syncs with delete enabled
    (scoped to that folder). Object materialisation folders sync without delete
    so unrelated Lakehouse content is never removed.
    """

    from ...fabric import sync as fabric_sync

    files_root = Path(files_root)
    target = _target(resolved)
    uploaded = 0
    for child in sorted(files_root.iterdir()):
        if not child.is_dir():
            continue
        if child.name == "_weaver":
            runtime = child / "runtime"
            if not runtime.is_dir():
                continue
            result = fabric_sync.sync_folder(
                target,
                runtime,
                "_weaver/runtime",
                respect_ignore=False,
                signatures=True,
                delete=True,
                degrees_of_parallelism=degrees_of_parallelism,
            )
        else:
            result = fabric_sync.sync_folder(
                target,
                child,
                child.name,
                respect_ignore=False,
                signatures=True,
                delete=False,
                degrees_of_parallelism=degrees_of_parallelism,
            )
        uploaded += result["files"]["uploaded"]
    return uploaded


def upload_files_tree(files_root: Path, resolved: dict, *, workers: int = 16) -> int:
    """Compatibility wrapper: upload a staged ``Files/`` tree to OneLake."""

    return sync_runtime_folder(files_root, resolved, degrees_of_parallelism=workers)


def delete_directory(resolved: dict, relative_path: str) -> bool:
    """Recursively delete a Lakehouse directory (``Files/<db>`` / ``Tables/<db>``)."""

    from ...fabric import onelake as fabric_onelake
    from ...fabric.client import FabricClientError

    try:
        return fabric_onelake.delete_directory(_target(resolved), relative_path)
    except FabricClientError as exc:
        raise BuildError(f"OneLake delete failed for {relative_path}: {exc}") from exc
