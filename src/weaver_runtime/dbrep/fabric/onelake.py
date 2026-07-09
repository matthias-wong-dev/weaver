"""OneLake resolution and upload for the Fabric Lakehouse backend."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from ..errors import BuildError


def _sync_folder():
    from weaver_runtime._legacy import load_script_module

    return load_script_module("sync_folder")


def _sync_git_repo():
    from weaver_runtime._legacy import load_script_module

    return load_script_module("sync_git_repo")


def resolve_lakehouse(workspace: str, lakehouse: str) -> dict:
    """Resolve workspace/lakehouse ids and a OneLake storage token."""

    sync_folder = _sync_folder()
    args = argparse.Namespace(
        workspace_id=None,
        workspace_name=workspace,
        lakehouse_id=None,
        lakehouse_name=lakehouse,
        api_base_url=sync_folder.DEFAULT_API_BASE_URL,
        fabric_scope=sync_folder.DEFAULT_FABRIC_SCOPE,
        storage_scope=sync_folder.DEFAULT_STORAGE_SCOPE,
    )
    workspace_id, workspace_name, lakehouse_payload, storage_token = (
        _sync_git_repo().resolve_fabric_target(args)
    )
    return {
        "workspace_id": workspace_id,
        "workspace_name": workspace_name,
        "lakehouse_id": str(lakehouse_payload["id"]),
        "lakehouse_name": lakehouse_payload.get("displayName"),
        "storage_token": storage_token,
        "onelake_base_url": sync_folder.DEFAULT_ONELAKE_BASE_URL,
    }


def upload_files_tree(files_root: Path, resolved: dict, *, workers: int = 16) -> int:
    """Upload every file under a local ``Files/`` staging tree to OneLake.

    ``files_path`` is the path relative to ``Files/`` (the OneLake DFS API adds
    the ``Files/`` prefix). Returns the number of files uploaded.
    """

    sync_folder = _sync_folder()
    files_root = Path(files_root)
    paths = [path for path in files_root.rglob("*") if path.is_file()]
    if not paths:
        return 0

    def _upload(path: Path) -> None:
        relative = path.relative_to(files_root).as_posix()
        sync_folder.upload_onelake_file(
            token=resolved["storage_token"],
            onelake_base_url=resolved["onelake_base_url"],
            workspace_id=resolved["workspace_id"],
            lakehouse_id=resolved["lakehouse_id"],
            files_path=relative,
            content=path.read_bytes(),
        )

    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(paths)))) as executor:
        futures = {executor.submit(_upload, path): path for path in paths}
        errors = [
            (futures[future], future.exception())
            for future in as_completed(futures)
            if future.exception() is not None
        ]
    if errors:
        path, exc = errors[0]
        raise BuildError(f"OneLake upload failed for {path}: {exc}") from exc
    return len(paths)
