"""Low-level OneLake DFS operations: URLs, upload, read, list, delete."""

from __future__ import annotations

import mimetypes
import os
import uuid
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlencode

from .client import FabricClientError, request_bytes, request_json
from .settings import DEFAULT_ONELAKE_BASE_URL

STORAGE_API_VERSION = "2023-11-03"


class OneLakeError(FabricClientError):
    """Raised when a OneLake path is unsafe or an operation fails."""


@dataclass(frozen=True)
class LakehouseTarget:
    """A resolved OneLake Lakehouse Files target plus the storage token."""

    workspace_id: str
    lakehouse_id: str
    storage_token: str
    onelake_base_url: str = DEFAULT_ONELAKE_BASE_URL
    workspace_name: str | None = None
    lakehouse_name: str | None = None


def normalise_files_folder(path: str) -> str:
    """Return a Lakehouse Files-relative folder path (``Files/`` stripped)."""

    normalised = path.strip().strip("/")
    if normalised == "Files":
        return ""
    if normalised.startswith("Files/"):
        normalised = normalised[len("Files/") :]
    parts = [part for part in normalised.split("/") if part]
    if any(part in {".", ".."} for part in parts):
        raise OneLakeError(f"unsafe target folder: {path!r}")
    return "/".join(parts)


def validate_relative_path(raw_path: str) -> str:
    """Validate a relative payload path before using it as a Lakehouse path."""

    path = raw_path.replace(os.sep, "/")
    parts = path.split("/")
    if (
        not path
        or path.startswith("/")
        or "\\" in path
        or any(part in {"", ".", ".."} for part in parts)
    ):
        raise OneLakeError(f"unsafe relative path: {raw_path!r}")
    return path


def artifact_path(lakehouse_id: str) -> str:
    """Return the OneLake path segment for one Lakehouse item."""

    try:
        uuid.UUID(lakehouse_id)
        return lakehouse_id
    except ValueError:
        return f"{lakehouse_id}.Lakehouse"


def file_url(
    onelake_base_url: str,
    workspace_id: str,
    lakehouse_id: str,
    files_path: str,
    query: dict[str, str] | None = None,
) -> str:
    """Return a OneLake DFS URL under one Lakehouse Files folder."""

    path = "/".join(
        quote(part, safe="")
        for part in [
            workspace_id,
            artifact_path(lakehouse_id),
            "Files",
            *[part for part in files_path.strip("/").split("/") if part],
        ]
    )
    url = f"{onelake_base_url.rstrip('/')}/{path}"
    if query:
        url = f"{url}?{urlencode(query)}"
    return url


def filesystem_url(
    onelake_base_url: str,
    workspace_id: str,
    query: dict[str, str] | None = None,
) -> str:
    """Return a OneLake DFS filesystem URL for path-list operations."""

    url = f"{onelake_base_url.rstrip('/')}/{quote(workspace_id, safe='')}"
    if query:
        url = f"{url}?{urlencode(query)}"
    return url


def _storage_headers(content_length: int = 0) -> dict[str, str]:
    return {
        "x-ms-version": STORAGE_API_VERSION,
        "Content-Length": str(content_length),
    }


def ensure_directory(target: LakehouseTarget, directory_path: str) -> None:
    """Create a Lakehouse Files directory (and its parents) if needed."""

    parts = [part for part in directory_path.strip("/").split("/") if part]
    for index in range(1, len(parts) + 1):
        current = "/".join(parts[:index])
        request_bytes(
            "PUT",
            file_url(
                target.onelake_base_url,
                target.workspace_id,
                target.lakehouse_id,
                current,
                {"resource": "directory"},
            ),
            target.storage_token,
            body=b"",
            headers=_storage_headers(),
            expected_statuses={200, 201, 409},
        )


def upload_file(
    target: LakehouseTarget,
    files_path: str,
    content: bytes,
    *,
    ensure_parent: bool = True,
) -> None:
    """Overwrite one Lakehouse Files object through the OneLake DFS API."""

    parent = "/".join(files_path.strip("/").split("/")[:-1])
    if parent and ensure_parent:
        ensure_directory(target, parent)

    request_bytes(
        "PUT",
        file_url(
            target.onelake_base_url,
            target.workspace_id,
            target.lakehouse_id,
            files_path,
            {"resource": "file", "overwrite": "true"},
        ),
        target.storage_token,
        body=b"",
        headers=_storage_headers(),
        expected_statuses={200, 201},
    )

    if content:
        request_bytes(
            "PATCH",
            file_url(
                target.onelake_base_url,
                target.workspace_id,
                target.lakehouse_id,
                files_path,
                {"action": "append", "position": "0"},
            ),
            target.storage_token,
            body=content,
            headers={
                **_storage_headers(len(content)),
                "Content-Type": mimetypes.guess_type(files_path)[0]
                or "application/octet-stream",
            },
            expected_statuses={202},
        )

    request_bytes(
        "PATCH",
        file_url(
            target.onelake_base_url,
            target.workspace_id,
            target.lakehouse_id,
            files_path,
            {"action": "flush", "position": str(len(content))},
        ),
        target.storage_token,
        body=b"",
        headers=_storage_headers(),
        expected_statuses={200},
    )


def read_file(target: LakehouseTarget, files_path: str) -> bytes:
    """Read one Lakehouse Files object through the OneLake DFS API."""

    content, _, _ = request_bytes(
        "GET",
        file_url(
            target.onelake_base_url, target.workspace_id, target.lakehouse_id, files_path
        ),
        target.storage_token,
        headers={"x-ms-version": STORAGE_API_VERSION},
    )
    return content


def list_files(target: LakehouseTarget, folder: str) -> list[dict[str, Any]]:
    """Return files listed recursively under one Lakehouse Files folder."""

    directory = "/".join(
        part
        for part in [
            artifact_path(target.lakehouse_id),
            "Files",
            *[part for part in folder.strip("/").split("/") if part],
        ]
        if part
    )
    paths: list[dict[str, Any]] = []
    continuation: str | None = None
    while True:
        query = {"resource": "filesystem", "directory": directory, "recursive": "true"}
        if continuation:
            query["continuation"] = continuation
        payload, headers, _ = request_json(
            "GET",
            filesystem_url(target.onelake_base_url, target.workspace_id, query),
            target.storage_token,
            expected_statuses={200},
        )
        payload = payload or {}
        paths.extend(
            path for path in payload.get("paths", []) if not path.get("isDirectory")
        )
        continuation = headers.get("x-ms-continuation")
        if not continuation:
            return paths


def delete_file(target: LakehouseTarget, files_path: str) -> None:
    """Delete one Lakehouse Files object when it exists."""

    request_bytes(
        "DELETE",
        file_url(
            target.onelake_base_url, target.workspace_id, target.lakehouse_id, files_path
        ),
        target.storage_token,
        headers={"x-ms-version": STORAGE_API_VERSION},
        expected_statuses={200, 202, 204, 404},
    )


def delete_directory(target: LakehouseTarget, relative_path: str) -> bool:
    """Recursively delete a Lakehouse directory server-side.

    Accepts a Files-relative or ``Tables/...`` path. Returns ``True`` if the
    directory existed, ``False`` if it was already absent.
    """

    path = relative_path.strip("/")
    base = target.onelake_base_url.rstrip("/")
    url = (
        f"{base}/{quote(target.workspace_id, safe='')}"
        f"/{quote(artifact_path(target.lakehouse_id), safe='')}/{path}?recursive=true"
    )
    _, _, status = request_bytes(
        "DELETE",
        url,
        target.storage_token,
        headers={"x-ms-version": STORAGE_API_VERSION},
        expected_statuses={200, 202, 204, 404},
    )
    return status != 404


def relative_payload_paths(
    remote_files: list[dict[str, Any]],
    lakehouse_id: str,
    folder: str,
    *,
    reserved: set[str] | None = None,
) -> set[str]:
    """Convert OneLake list results into paths relative to ``folder``."""

    reserved = reserved or set()
    remote_prefix = "/".join(
        part
        for part in [
            artifact_path(lakehouse_id),
            "Files",
            *[part for part in folder.strip("/").split("/") if part],
        ]
        if part
    )
    remote_prefix_with_slash = f"{remote_prefix}/"
    paths: set[str] = set()
    for remote_file in remote_files:
        remote_name = str(remote_file.get("name", "")).strip("/")
        if remote_name == remote_prefix:
            relative = os.path.basename(remote_name)
        elif remote_name.startswith(remote_prefix_with_slash):
            relative = remote_name[len(remote_prefix_with_slash) :]
        else:
            raise OneLakeError(f"unexpected remote file outside target: {remote_name}")
        if relative in reserved:
            continue
        paths.add(validate_relative_path(relative))
    return paths
