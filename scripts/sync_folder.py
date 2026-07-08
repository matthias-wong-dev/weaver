#!/usr/bin/env python3
"""Sync folders between local disk and a Microsoft Fabric Lakehouse Files folder."""

from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from azure.identity import DefaultAzureCredential


DEFAULT_API_BASE_URL = "https://api.fabric.microsoft.com"
DEFAULT_ONELAKE_BASE_URL = "https://onelake.dfs.fabric.microsoft.com"
DEFAULT_FABRIC_SCOPE = "https://analysis.windows.net/powerbi/api/.default"
DEFAULT_STORAGE_SCOPE = "https://storage.azure.com/.default"
DEFAULT_LOCAL_DIR = Path(".")
DEFAULT_LAKEHOUSE_FOLDER = "platform"
DEFAULT_WORKSPACE_NAME = None
DEFAULT_LAKEHOUSE_NAME = None
SYNC_SUFFIXES = {".py", ".json", ".txt", ".md", ".toml", ".yaml", ".yml"}
GET_STATUSES = {200}
DEFAULT_WORKERS = 32
TRANSIENT_HTTP_STATUSES = {408, 429, 500, 502, 503, 504}
MAX_REQUEST_ATTEMPTS = 4


class SyncError(RuntimeError):
    """Raised when folder sync fails."""


def get_access_token(scope: str) -> str:
    """Return an Azure access token for one API scope."""

    return DefaultAzureCredential().get_token(scope).token


def request_bytes(
    method: str,
    url: str,
    token: str,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
    expected_statuses: set[int] | None = None,
) -> tuple[bytes, dict[str, str], int]:
    """Call a REST endpoint and return response bytes, headers, and status."""

    expected_statuses = expected_statuses or GET_STATUSES
    request_headers = {
        "Authorization": f"Bearer {token}",
        **(headers or {}),
    }

    for attempt in range(1, MAX_REQUEST_ATTEMPTS + 1):
        request = Request(url, data=body, headers=request_headers, method=method)
        try:
            with urlopen(request, timeout=120) as response:
                response_body = response.read()
                response_headers = {key.lower(): value for key, value in response.headers.items()}

                if response.status not in expected_statuses:
                    if response.status in TRANSIENT_HTTP_STATUSES and attempt < MAX_REQUEST_ATTEMPTS:
                        time.sleep(min(2 ** attempt, 10))
                        continue
                    raise SyncError(
                        f"{method} {url} returned HTTP {response.status}: "
                        f"{response_body.decode('utf-8', errors='replace')}"
                    )

                return response_body, response_headers, response.status
        except HTTPError as exc:
            response_body = exc.read()
            response_headers = {key.lower(): value for key, value in exc.headers.items()}
            if exc.code in expected_statuses:
                return response_body, response_headers, exc.code
            if exc.code in TRANSIENT_HTTP_STATUSES and attempt < MAX_REQUEST_ATTEMPTS:
                time.sleep(min(2 ** attempt, 10))
                continue
            detail = response_body.decode("utf-8", errors="replace")
            raise SyncError(f"{method} {url} returned HTTP {exc.code}: {detail}") from exc
        except URLError as exc:
            reason = str(exc.reason).lower()
            if attempt < MAX_REQUEST_ATTEMPTS and (
                "timed out" in reason
                or "connection reset" in reason
                or "connection aborted" in reason
                or "remote end closed" in reason
            ):
                time.sleep(min(2 ** attempt, 10))
                continue
            raise SyncError(f"{method} {url} failed: {exc.reason}") from exc

    raise SyncError(f"{method} {url} failed after {MAX_REQUEST_ATTEMPTS} attempts")


def request_json(
    method: str,
    url: str,
    token: str,
    payload: dict[str, Any] | None = None,
    expected_statuses: set[int] | None = None,
) -> tuple[dict[str, Any] | None, dict[str, str], int]:
    """Call a JSON REST endpoint and return payload, headers, and status."""

    body = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {
        "Accept": "application/json",
        **({"Content-Type": "application/json"} if body is not None else {}),
    }
    response_body, response_headers, status = request_bytes(
        method,
        url,
        token,
        body=body,
        headers=headers,
        expected_statuses=expected_statuses,
    )

    return (
        json.loads(response_body.decode("utf-8")) if response_body else None,
        response_headers,
        status,
    )


def fabric_url(api_base_url: str, path: str) -> str:
    """Return an absolute Fabric API URL."""

    return f"{api_base_url.rstrip('/')}/v1/{path.lstrip('/')}"


def workspace_url(api_base_url: str, workspace_id: str) -> str:
    """Return one workspace endpoint URL."""

    return fabric_url(api_base_url, f"workspaces/{workspace_id}")


def workspace_items_url(api_base_url: str, workspace_id: str) -> str:
    """Return the workspace items endpoint URL."""

    return fabric_url(api_base_url, f"workspaces/{workspace_id}/items")


def list_items(
    token: str,
    workspace_id: str,
    api_base_url: str = DEFAULT_API_BASE_URL,
) -> list[dict[str, Any]]:
    """Return all items in a Fabric workspace."""

    items = []
    next_url = workspace_items_url(api_base_url, workspace_id)

    while next_url:
        payload, _, _ = request_json("GET", next_url, token)
        payload = payload or {}
        items.extend(payload.get("value", []))
        next_url = payload.get("continuationUri") or payload.get("nextLink")

    return items


def find_workspace_id(
    token: str,
    workspace_name: str,
    api_base_url: str = DEFAULT_API_BASE_URL,
) -> str:
    """Find a Fabric workspace ID by display name."""

    next_url = fabric_url(api_base_url, "workspaces")

    while next_url:
        payload, _, _ = request_json("GET", next_url, token)
        payload = payload or {}
        for workspace in payload.get("value", []):
            if workspace.get("displayName") == workspace_name:
                return workspace["id"]

        next_url = payload.get("continuationUri") or payload.get("nextLink")

    raise SyncError(f"workspace not found: {workspace_name!r}")


def workspace_display_name(
    token: str,
    workspace_id: str,
    api_base_url: str = DEFAULT_API_BASE_URL,
) -> str | None:
    """Return the workspace display name."""

    payload, _, _ = request_json("GET", workspace_url(api_base_url, workspace_id), token)
    return (payload or {}).get("displayName")


def resolve_lakehouse(
    token: str,
    workspace_id: str,
    lakehouse_id: str | None = None,
    lakehouse_name: str | None = None,
    api_base_url: str = DEFAULT_API_BASE_URL,
) -> dict[str, Any]:
    """Resolve the Lakehouse item to use for syncing Files."""

    lakehouses = [
        item
        for item in list_items(token, workspace_id, api_base_url)
        if item.get("type") == "Lakehouse"
    ]

    if lakehouse_id:
        for lakehouse in lakehouses:
            if lakehouse.get("id") == lakehouse_id:
                return lakehouse
        raise SyncError(f"lakehouse id not found: {lakehouse_id!r}")

    if lakehouse_name:
        for lakehouse in lakehouses:
            if lakehouse.get("displayName") == lakehouse_name:
                return lakehouse
        raise SyncError(f"lakehouse name not found: {lakehouse_name!r}")

    if len(lakehouses) == 1:
        return lakehouses[0]

    names = [lakehouse.get("displayName") for lakehouse in lakehouses]
    raise SyncError(f"provide --lakehouse-id or --lakehouse-name; found lakehouses: {names}")


def iter_local_files(local_dir: Path) -> list[Path]:
    """Return local files to sync."""

    return [
        path
        for path in sorted(local_dir.rglob("*"))
        if path.is_file()
        and "__pycache__" not in path.parts
        and path.suffix in SYNC_SUFFIXES
    ]


def local_relative_path(local_dir: Path, file_path: Path) -> str:
    """Return a local file path relative to the synced folder."""

    return file_path.relative_to(local_dir).as_posix()


def onelake_file_url(
    onelake_base_url: str,
    workspace_id: str,
    lakehouse_id: str,
    files_path: str,
    query: dict[str, str] | None = None,
) -> str:
    """Return a OneLake DFS URL under one Lakehouse Files folder."""

    try:
        uuid.UUID(lakehouse_id)
        artifact_path = lakehouse_id
    except ValueError:
        artifact_path = f"{lakehouse_id}.Lakehouse"

    path = "/".join(
        quote(part, safe="")
        for part in [
            workspace_id,
            artifact_path,
            "Files",
            *[part for part in files_path.strip("/").split("/") if part],
        ]
    )
    url = f"{onelake_base_url.rstrip('/')}/{path}"

    if query:
        url = f"{url}?{urlencode(query)}"

    return url


def onelake_artifact_path(lakehouse_id: str) -> str:
    """Return the path segment used for one Lakehouse item in OneLake."""

    try:
        uuid.UUID(lakehouse_id)
        return lakehouse_id
    except ValueError:
        return f"{lakehouse_id}.Lakehouse"


def onelake_filesystem_url(
    onelake_base_url: str,
    workspace_id: str,
    query: dict[str, str] | None = None,
) -> str:
    """Return a OneLake DFS filesystem URL for path-list operations."""

    url = f"{onelake_base_url.rstrip('/')}/{quote(workspace_id, safe='')}"
    if query:
        url = f"{url}?{urlencode(query)}"

    return url


def storage_headers(content_length: int = 0) -> dict[str, str]:
    """Return common OneLake DFS request headers."""

    return {
        "x-ms-version": "2023-11-03",
        "Content-Length": str(content_length),
    }


def ensure_onelake_directory(
    token: str,
    onelake_base_url: str,
    workspace_id: str,
    lakehouse_id: str,
    directory_path: str,
) -> None:
    """Create a Lakehouse Files directory if needed."""

    parts = [part for part in directory_path.strip("/").split("/") if part]

    for index in range(1, len(parts) + 1):
        current = "/".join(parts[:index])
        request_bytes(
            "PUT",
            onelake_file_url(
                onelake_base_url,
                workspace_id,
                lakehouse_id,
                current,
                {"resource": "directory"},
            ),
            token,
            body=b"",
            headers=storage_headers(),
            expected_statuses={200, 201, 409},
        )


def upload_onelake_file(
    token: str,
    onelake_base_url: str,
    workspace_id: str,
    lakehouse_id: str,
    files_path: str,
    content: bytes,
    ensure_parent: bool = True,
) -> None:
    """Overwrite one Lakehouse Files object through the OneLake DFS API."""

    parent = "/".join(files_path.strip("/").split("/")[:-1])

    if parent and ensure_parent:
        ensure_onelake_directory(token, onelake_base_url, workspace_id, lakehouse_id, parent)

    create_url = onelake_file_url(
        onelake_base_url,
        workspace_id,
        lakehouse_id,
        files_path,
        {"resource": "file", "overwrite": "true"},
    )
    request_bytes(
        "PUT",
        create_url,
        token,
        body=b"",
        headers=storage_headers(),
        expected_statuses={200, 201},
    )

    if content:
        append_url = onelake_file_url(
            onelake_base_url,
            workspace_id,
            lakehouse_id,
            files_path,
            {"action": "append", "position": "0"},
        )
        request_bytes(
            "PATCH",
            append_url,
            token,
            body=content,
            headers={
                **storage_headers(len(content)),
                "Content-Type": mimetypes.guess_type(files_path)[0] or "application/octet-stream",
            },
            expected_statuses={202},
        )

    flush_url = onelake_file_url(
        onelake_base_url,
        workspace_id,
        lakehouse_id,
        files_path,
        {"action": "flush", "position": str(len(content))},
    )
    request_bytes(
        "PATCH",
        flush_url,
        token,
        body=b"",
        headers=storage_headers(),
        expected_statuses={200},
    )


def read_onelake_file(
    token: str,
    onelake_base_url: str,
    workspace_id: str,
    lakehouse_id: str,
    files_path: str,
) -> bytes:
    """Read one Lakehouse Files object through the OneLake DFS API."""

    content, _, _ = request_bytes(
        "GET",
        onelake_file_url(onelake_base_url, workspace_id, lakehouse_id, files_path),
        token,
        headers={"x-ms-version": "2023-11-03"},
    )
    return content


def list_onelake_files(
    token: str,
    onelake_base_url: str,
    workspace_id: str,
    lakehouse_id: str,
    lakehouse_folder: str,
) -> list[dict[str, Any]]:
    """Return files listed under one Lakehouse Files folder."""

    artifact_path = onelake_artifact_path(lakehouse_id)
    directory = "/".join(
        part
        for part in [
            artifact_path,
            "Files",
            *[part for part in lakehouse_folder.strip("/").split("/") if part],
        ]
        if part
    )
    paths: list[dict[str, Any]] = []
    continuation: str | None = None

    while True:
        query = {
            "resource": "filesystem",
            "directory": directory,
            "recursive": "true",
        }
        if continuation:
            query["continuation"] = continuation

        payload, headers, _ = request_json(
            "GET",
            onelake_filesystem_url(onelake_base_url, workspace_id, query),
            token,
            expected_statuses={200},
        )
        payload = payload or {}
        paths.extend(
            path
            for path in payload.get("paths", [])
            if not path.get("isDirectory")
        )

        continuation = headers.get("x-ms-continuation")
        if not continuation:
            return paths


def download_onelake_folder(
    token: str,
    workspace_id: str,
    lakehouse_id: str,
    local_dir: Path,
    lakehouse_folder: str,
    onelake_base_url: str = DEFAULT_ONELAKE_BASE_URL,
    overwrite: bool = False,
    workers: int = DEFAULT_WORKERS,
) -> dict[str, Any]:
    """Download all files from one Lakehouse Files folder."""

    artifact_path = onelake_artifact_path(lakehouse_id)
    remote_prefix = "/".join(
        part
        for part in [
            artifact_path,
            "Files",
            *[part for part in lakehouse_folder.strip("/").split("/") if part],
        ]
        if part
    )
    remote_prefix_with_slash = f"{remote_prefix}/"
    remote_files = list_onelake_files(
        token=token,
        onelake_base_url=onelake_base_url,
        workspace_id=workspace_id,
        lakehouse_id=lakehouse_id,
        lakehouse_folder=lakehouse_folder,
    )
    downloaded = []
    skipped = []

    def download_one(remote_file: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        remote_name = remote_file["name"]
        if remote_name == remote_prefix:
            relative_path = Path(remote_name).name
        elif remote_name.startswith(remote_prefix_with_slash):
            relative_path = remote_name[len(remote_prefix_with_slash):]
        else:
            raise SyncError(f"unexpected OneLake path outside requested folder: {remote_name}")

        local_path = local_dir / Path(relative_path)
        expected_size = int(remote_file.get("contentLength") or -1)

        if (
            not overwrite
            and local_path.exists()
            and expected_size >= 0
            and local_path.stat().st_size == expected_size
        ):
            return (
                "skipped",
                {
                    "local_path": str(local_path),
                    "source_path": f"Files/{lakehouse_folder.strip('/')}/{relative_path}",
                    "bytes": expected_size,
                    "reason": "same_size",
                },
            )

        content = read_onelake_file(
            token=token,
            onelake_base_url=onelake_base_url,
            workspace_id=workspace_id,
            lakehouse_id=lakehouse_id,
            files_path=f"{lakehouse_folder.strip('/')}/{relative_path}",
        )
        local_path.parent.mkdir(parents=True, exist_ok=True)
        local_path.write_bytes(content)
        return (
            "downloaded",
            {
                "local_path": str(local_path),
                "source_path": f"Files/{lakehouse_folder.strip('/')}/{relative_path}",
                "bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            },
        )

    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        futures = [executor.submit(download_one, remote_file) for remote_file in remote_files]
        for future in as_completed(futures):
            status, record = future.result()
            if status == "skipped":
                skipped.append(record)
            else:
                downloaded.append(record)

    return {
        "local_dir": str(local_dir),
        "source": f"Files/{lakehouse_folder.strip('/')}",
        "listed": len(remote_files),
        "downloaded": len(downloaded),
        "skipped": len(skipped),
        "files": downloaded,
        "skipped_files": skipped,
        "success": True,
    }


def sync_lakehouse_folder(
    token: str,
    workspace_id: str,
    lakehouse_id: str,
    local_dir: Path,
    lakehouse_folder: str = DEFAULT_LAKEHOUSE_FOLDER,
    onelake_base_url: str = DEFAULT_ONELAKE_BASE_URL,
    verify: bool = True,
) -> dict[str, Any]:
    """Upload local folder files into a Lakehouse Files folder and verify bytes."""

    if not local_dir.exists():
        raise FileNotFoundError(f"local directory not found: {local_dir}")
    if not local_dir.is_dir():
        raise NotADirectoryError(f"local path is not a directory: {local_dir}")

    synced = []

    for file_path in iter_local_files(local_dir):
        relative_path = local_relative_path(local_dir, file_path)
        target_path = f"{lakehouse_folder.strip('/')}/{relative_path}"
        content = file_path.read_bytes()

        upload_onelake_file(
            token=token,
            onelake_base_url=onelake_base_url,
            workspace_id=workspace_id,
            lakehouse_id=lakehouse_id,
            files_path=target_path,
            content=content,
        )

        sha256 = hashlib.sha256(content).hexdigest()

        if verify:
            remote_content = read_onelake_file(
                token=token,
                onelake_base_url=onelake_base_url,
                workspace_id=workspace_id,
                lakehouse_id=lakehouse_id,
                files_path=target_path,
            )

            if remote_content != content:
                raise SyncError(f"uploaded file verification failed: Files/{target_path}")

        synced.append(
            {
                "local_path": str(file_path),
                "target_path": f"Files/{target_path}",
                "bytes": len(content),
                "sha256": sha256,
            }
        )

    return {
        "local_dir": str(local_dir),
        "target": f"Files/{lakehouse_folder.strip('/')}",
        "synced": len(synced),
        "files": synced,
        "verified": verify,
        "success": True,
    }


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""

    parser = argparse.ArgumentParser(description=__doc__)
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
    parser.add_argument("--local-dir", type=Path, default=DEFAULT_LOCAL_DIR)
    parser.add_argument("--lakehouse-folder", default=DEFAULT_LAKEHOUSE_FOLDER)
    parser.add_argument("--api-base-url", default=os.environ.get("FABRIC_API_BASE_URL", DEFAULT_API_BASE_URL))
    parser.add_argument("--onelake-base-url", default=os.environ.get("ONELAKE_BASE_URL", DEFAULT_ONELAKE_BASE_URL))
    parser.add_argument("--fabric-scope", default=os.environ.get("FABRIC_API_SCOPE", DEFAULT_FABRIC_SCOPE))
    parser.add_argument("--storage-scope", default=os.environ.get("ONELAKE_SCOPE", DEFAULT_STORAGE_SCOPE))
    parser.add_argument("--no-verify", action="store_true", help="Skip read-back verification.")
    parser.add_argument("--direction", choices=["upload", "download"], default="upload")
    parser.add_argument("--overwrite-downloads", action="store_true")
    parser.add_argument("--download-workers", type=int, default=DEFAULT_WORKERS)
    parser.add_argument("--dry-run", action="store_true", help="Print planned sync without calling Fabric or OneLake.")
    return parser.parse_args()


def sync_plan(local_dir: Path, lakehouse_folder: str) -> dict[str, Any]:
    """Return the local files that would be synced."""

    return {
        "local_dir": str(local_dir),
        "target": f"Files/{lakehouse_folder.strip('/')}",
        "files": [
            {
                "path": local_relative_path(local_dir, path),
                "bytes": path.stat().st_size,
            }
            for path in iter_local_files(local_dir)
        ],
    }


def main() -> int:
    """CLI entrypoint."""

    args = parse_args()
    planned = (
        sync_plan(args.local_dir, args.lakehouse_folder)
        if args.direction == "upload"
        else {
            "local_dir": str(args.local_dir),
            "source": f"Files/{args.lakehouse_folder.strip('/')}",
            "direction": "download",
        }
    )

    if args.dry_run:
        print(json.dumps(planned, indent=2), flush=True)
        return 0

    if not args.workspace_id and not args.workspace_name:
        raise SyncError(
            "provide --workspace-id or --workspace-name "
            "(or set FABRIC_WORKSPACE_ID or FABRIC_WORKSPACE_NAME)"
        )

    fabric_token = get_access_token(args.fabric_scope)
    if args.workspace_id:
        workspace_id = args.workspace_id
    else:
        workspace_id = find_workspace_id(fabric_token, args.workspace_name, args.api_base_url)

    workspace_name = workspace_display_name(fabric_token, workspace_id, args.api_base_url)
    lakehouse = resolve_lakehouse(
        token=fabric_token,
        workspace_id=workspace_id,
        lakehouse_id=args.lakehouse_id,
        lakehouse_name=args.lakehouse_name,
        api_base_url=args.api_base_url,
    )
    storage_token = get_access_token(args.storage_scope)
    sync_result = (
        sync_lakehouse_folder(
            token=storage_token,
            workspace_id=workspace_id,
            lakehouse_id=lakehouse["id"],
            local_dir=args.local_dir,
            lakehouse_folder=args.lakehouse_folder,
            onelake_base_url=args.onelake_base_url,
            verify=not args.no_verify,
        )
        if args.direction == "upload"
        else download_onelake_folder(
            token=storage_token,
            workspace_id=workspace_id,
            lakehouse_id=lakehouse["id"],
            local_dir=args.local_dir,
            lakehouse_folder=args.lakehouse_folder,
            onelake_base_url=args.onelake_base_url,
            overwrite=args.overwrite_downloads,
            workers=args.download_workers,
        )
    )
    result = {
        "workspace_id": workspace_id,
        "workspace_name": workspace_name,
        "lakehouse": lakehouse,
        "plan": planned,
        "sync": sync_result,
    }

    print(json.dumps(result, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SyncError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
