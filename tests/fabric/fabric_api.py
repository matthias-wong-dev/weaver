"""Fabric REST helpers to create/delete test warehouses and lakehouses.

Used only by opt-in Fabric tests so they can provision disposable resources on
demand (and tear them down), rather than depending on pre-created items.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
import uuid

from azure.identity import DefaultAzureCredential

_API = "https://api.fabric.microsoft.com/v1"
_SCOPE = "https://api.fabric.microsoft.com/.default"


def fabric_token() -> str:
    return DefaultAzureCredential().get_token(_SCOPE).token


def unique_name(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


def _request(method: str, url: str, token: str, body: dict | None = None):
    data = None if body is None else json.dumps(body).encode()
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    if data:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            raw = response.read().decode()
            headers_lower = {key.lower(): value for key, value in response.headers.items()}
            return response.status, headers_lower, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode(errors="replace")
        raise RuntimeError(f"{method} {url} -> {exc.code}: {raw[:300]}") from exc


def _paged(url: str, token: str) -> list[dict]:
    values: list[dict] = []
    while url:
        _, _, payload = _request("GET", url, token)
        values.extend(payload.get("value", []))
        url = payload.get("continuationUri")
    return values


def resolve_workspace(token: str, name_or_id: str) -> tuple[str, str]:
    for workspace in _paged(f"{_API}/workspaces", token):
        if workspace.get("id") == name_or_id or workspace.get("displayName") == name_or_id:
            return workspace["id"], workspace["displayName"]
    raise RuntimeError(f"workspace {name_or_id!r} not found")


def _wait_lro(token: str, poll_url: str, timeout: float = 600.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        _, _, body = _request("GET", poll_url, token)
        status = body.get("status")
        if status == "Succeeded":
            _, _, result = _request("GET", poll_url.rstrip("/") + "/result", token)
            return result or body
        if status == "Failed":
            raise RuntimeError(f"Fabric operation failed: {body}")
        time.sleep(5)
    raise RuntimeError("Fabric operation timed out")


def _create_item(token: str, workspace_id: str, kind: str, name: str) -> dict:
    status, headers, body = _request(
        "POST", f"{_API}/workspaces/{workspace_id}/{kind}", token, {"displayName": name}
    )
    if status in (200, 201):
        return body
    if status == 202:
        poll = headers.get("operation-location") or headers.get("location")
        if not poll:
            raise RuntimeError(f"create {kind} {name}: 202 without a poll URL")
        return _wait_lro(token, poll)
    raise RuntimeError(f"create {kind} {name} -> {status}: {body}")


def create_warehouse(token: str, workspace_id: str, name: str) -> dict:
    item = _create_item(token, workspace_id, "warehouses", name)
    warehouse_id = item["id"]
    _, _, full = _request(
        "GET", f"{_API}/workspaces/{workspace_id}/warehouses/{warehouse_id}", token
    )
    connection_string = (full.get("properties") or {}).get("connectionString")
    return {"id": warehouse_id, "name": name, "connection_string": connection_string}


def create_lakehouse(token: str, workspace_id: str, name: str) -> dict:
    item = _create_item(token, workspace_id, "lakehouses", name)
    return {"id": item["id"], "name": name}


def delete_item(token: str, workspace_id: str, kind: str, item_id: str) -> None:
    try:
        _request("DELETE", f"{_API}/workspaces/{workspace_id}/{kind}/{item_id}", token)
    except RuntimeError:
        pass  # best-effort teardown
