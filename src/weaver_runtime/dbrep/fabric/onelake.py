"""OneLake resolution + upload for the Fabric Lakehouse dbrep backend.

Thin bridge over the shared :mod:`weaver_runtime.fabric` substrate. Runtime file
movement goes through the shared folder-sync layer (signature diff, scoped
delete); ``resolve_lakehouse`` returns a plain dict for backward compatibility.
"""

from __future__ import annotations

import json
from pathlib import Path

from ..errors import BuildError

RUNTIME_METADATA_NAMES = (
    "manifest.json",
    "catalogue.json",
    "load_dependency.json",
    "table_dictionary.json",
    "column_dictionary.json",
    "index_dictionary.json",
    "foreign_key_dictionary.json",
)


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
    files_root: Path,
    resolved: dict,
    *,
    runtime_components=(),
    degrees_of_parallelism: int = 32,
) -> int:
    """Sync materialisations and independently owned runtime components.

    Deletion is enabled only within the orchestrator and each selected database
    snapshot. Shared metadata is uploaded file-by-file after it has been merged
    with the remote documents by the caller.
    """

    from ...fabric import sync as fabric_sync
    from ...fabric.ignore import default_platform_ignore_spec

    files_root = Path(files_root)
    target = _target(resolved)
    uploaded = 0
    runtime = files_root / "_weaver" / "runtime"
    components = tuple(runtime_components)
    if not components:
        # Compatibility for direct callers while retaining safe component roots.
        from ..lakehouse.artifacts import RuntimeComponent

        components = (
            RuntimeComponent(
                "builtin",
                "weaver",
                runtime / "_orchestrator",
                "_weaver/runtime/_orchestrator",
            ),
            *(
                RuntimeComponent(
                    "database",
                    child.name,
                    child,
                    f"_weaver/runtime/objects/{child.name}",
                )
                for child in sorted((runtime / "objects").iterdir())
                if child.is_dir()
            ),
        )

    for component in components:
        result = fabric_sync.sync_folder(
            target,
            component.local_root,
            component.remote_root,
            respect_ignore=False,
            extra_ignore=default_platform_ignore_spec(),
            signatures=True,
            delete=True,
            degrees_of_parallelism=degrees_of_parallelism,
        )
        uploaded += result["files"]["uploaded"]

    for name in RUNTIME_METADATA_NAMES:
        path = runtime / name
        upload_file(resolved, f"_weaver/runtime/{name}", path.read_bytes())
        uploaded += 1

    for child in sorted(files_root.iterdir()):
        if not child.is_dir():
            continue
        if child.name == "_weaver":
            continue
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


def read_runtime_metadata(resolved: dict) -> dict[str, dict]:
    """Read only the small shared runtime documents that need partial merging."""

    from ...fabric import onelake as fabric_onelake
    from ...fabric.client import FabricClientError

    target = _target(resolved)
    documents: dict[str, dict] = {}
    for name in RUNTIME_METADATA_NAMES:
        path = f"_weaver/runtime/{name}"
        try:
            content = fabric_onelake.read_file(target, path)
        except FabricClientError as exc:
            if "returned HTTP 404" in str(exc):
                continue
            raise BuildError(f"OneLake metadata read failed for {path}: {exc}") from exc
        try:
            documents[name] = json.loads(content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BuildError(
                f"installed runtime metadata is invalid JSON: {path}: {exc}"
            ) from exc
    return documents


def upload_files_tree(files_root: Path, resolved: dict, *, workers: int = 16) -> int:
    """Compatibility wrapper: upload a staged ``Files/`` tree to OneLake."""

    return sync_runtime_folder(files_root, resolved, degrees_of_parallelism=workers)


def upload_file(resolved: dict, files_path: str, content: bytes) -> None:
    """Overwrite one Lakehouse ``Files/``-relative object (e.g. a small record)."""

    from ...fabric import onelake as fabric_onelake

    fabric_onelake.upload_file(_target(resolved), files_path, content)


def delete_directory(resolved: dict, relative_path: str) -> bool:
    """Recursively delete a Lakehouse directory (``Files/<db>`` / ``Tables/<db>``)."""

    from ...fabric import onelake as fabric_onelake
    from ...fabric.client import FabricClientError

    try:
        return fabric_onelake.delete_directory(_target(resolved), relative_path)
    except FabricClientError as exc:
        raise BuildError(f"OneLake delete failed for {relative_path}: {exc}") from exc
