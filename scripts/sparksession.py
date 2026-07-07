#!/usr/bin/env python3
"""Run Python code in a Microsoft Fabric Spark Livy session."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from azure.identity import DefaultAzureCredential


DEFAULT_API_BASE_URL = "https://api.fabric.microsoft.com"
DEFAULT_API_VERSION = "2023-12-01"
DEFAULT_SCOPE = "https://analysis.windows.net/powerbi/api/.default"
DEFAULT_WORKSPACE_NAME = "I Love Government"
DEFAULT_LAKEHOUSE_NAME = "T1"
DEFAULT_CODE = "\n".join(
    [
        'df = spark.createDataFrame([("hello world",)], ["message"])',
        'print(df.collect()[0]["message"])',
    ]
)
TERMINAL_SESSION_STATES = {"dead", "error", "killed", "cancelled", "canceled"}
TERMINAL_STATEMENT_STATES = {"error", "cancelled", "canceled"}


class FabricLivyError(RuntimeError):
    """Raised when a Fabric Livy request fails."""


def get_access_token(scope: str = DEFAULT_SCOPE) -> str:
    credential = DefaultAzureCredential()
    return credential.get_token(scope).token


def livy_sessions_url(
    workspace_id: str,
    lakehouse_id: str,
    api_base_url: str = DEFAULT_API_BASE_URL,
    api_version: str = DEFAULT_API_VERSION,
) -> str:
    return (
        f"{api_base_url.rstrip('/')}/v1"
        f"/workspaces/{workspace_id}"
        f"/lakehouses/{lakehouse_id}"
        f"/livyapi/versions/{api_version}"
        "/sessions"
    )


def fabric_url(api_base_url: str, path: str) -> str:
    return f"{api_base_url.rstrip('/')}/v1/{path.lstrip('/')}"


def request_json(
    method: str,
    url: str,
    token: str,
    payload: dict[str, Any] | None = None,
    expected_statuses: set[int] | None = None,
) -> dict[str, Any]:
    expected_statuses = expected_statuses or {200}
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }
    if body is not None:
        headers["Content-Type"] = "application/json"

    request = Request(url, data=body, headers=headers, method=method)
    try:
        with urlopen(request, timeout=60) as response:
            response_body = response.read().decode("utf-8")
            if response.status not in expected_statuses:
                raise FabricLivyError(
                    f"{method} {url} returned HTTP {response.status}: {response_body}"
                )
            if not response_body:
                return {}
            return json.loads(response_body)
    except HTTPError as exc:
        response_body = exc.read().decode("utf-8", errors="replace")
        raise FabricLivyError(f"{method} {url} returned HTTP {exc.code}: {response_body}") from exc
    except URLError as exc:
        raise FabricLivyError(f"{method} {url} failed: {exc.reason}") from exc


def paged_values(url: str, token: str) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    while url:
        payload = request_json("GET", url, token)
        values.extend(payload.get("value", []))
        url = payload.get("continuationUri") or payload.get("nextLink") or ""
    return values


def resolve_workspace_id(api_base_url: str, token: str, workspace_name: str) -> str:
    workspaces = paged_values(fabric_url(api_base_url, "workspaces"), token)
    matches = [
        workspace
        for workspace in workspaces
        if workspace.get("displayName") == workspace_name
    ]
    if len(matches) != 1:
        available = sorted(str(workspace.get("displayName")) for workspace in workspaces)
        raise FabricLivyError(
            f"expected one workspace named {workspace_name!r}, found {len(matches)}; "
            f"available workspaces: {available}"
        )
    return str(matches[0]["id"])


def resolve_lakehouse_id(
    api_base_url: str,
    token: str,
    workspace_id: str,
    lakehouse_name: str | None,
) -> str:
    items_url = fabric_url(api_base_url, f"workspaces/{quote(workspace_id)}/items")
    lakehouses = [
        item
        for item in paged_values(items_url, token)
        if item.get("type") == "Lakehouse"
    ]

    if lakehouse_name:
        matches = [
            lakehouse
            for lakehouse in lakehouses
            if lakehouse.get("displayName") == lakehouse_name
        ]
        if len(matches) != 1:
            available = sorted(str(lakehouse.get("displayName")) for lakehouse in lakehouses)
            raise FabricLivyError(
                f"expected one lakehouse named {lakehouse_name!r}, found {len(matches)}; "
                f"available lakehouses: {available}"
            )
        return str(matches[0]["id"])

    if len(lakehouses) == 1:
        return str(lakehouses[0]["id"])

    available = sorted(str(lakehouse.get("displayName")) for lakehouse in lakehouses)
    raise FabricLivyError(f"provide --lakehouse-id or --lakehouse-name; found lakehouses: {available}")


def delete_session(url: str, token: str) -> None:
    request_json("DELETE", url, token, expected_statuses={200, 202, 204})


def create_session(
    sessions_url: str,
    token: str,
    environment_id: str | None = None,
    conf: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    session_conf = dict(conf or {})
    if environment_id:
        session_conf["spark.fabric.environmentDetails"] = json.dumps({"id": environment_id})
    if session_conf:
        payload["conf"] = session_conf
    return request_json("POST", sessions_url, token, payload, expected_statuses={200, 201, 202})


def wait_for_session_idle(
    session_url: str,
    token: str,
    poll_interval: float,
    timeout: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while True:
        session = request_json("GET", session_url, token)
        state = str(session.get("state", "")).lower()
        if state == "idle":
            return session
        if state in TERMINAL_SESSION_STATES:
            raise FabricLivyError(f"Livy session entered terminal state {state!r}: {session}")
        if time.monotonic() >= deadline:
            raise FabricLivyError(f"Timed out waiting for Livy session to become idle: {session}")
        print(f"session state: {session.get('state', 'unknown')}", file=sys.stderr, flush=True)
        time.sleep(poll_interval)


def submit_statement(session_url: str, token: str, code: str, kind: str) -> dict[str, Any]:
    payload = {"code": code, "kind": kind}
    return request_json(
        "POST",
        f"{session_url}/statements",
        token,
        payload,
        expected_statuses={200, 201, 202},
    )


def wait_for_statement(
    statement_url: str,
    token: str,
    poll_interval: float,
    timeout: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while True:
        statement = request_json("GET", statement_url, token)
        state = str(statement.get("state", "")).lower()
        if state == "available":
            return statement
        if state in TERMINAL_STATEMENT_STATES:
            raise FabricLivyError(f"Livy statement entered terminal state {state!r}: {statement}")
        if time.monotonic() >= deadline:
            raise FabricLivyError(f"Timed out waiting for Livy statement: {statement}")
        print(f"statement state: {statement.get('state', 'unknown')}", file=sys.stderr, flush=True)
        time.sleep(poll_interval)


def print_statement_output(statement: dict[str, Any], show_json: bool) -> None:
    if show_json:
        print(json.dumps(statement, indent=2), flush=True)
        return

    output = statement.get("output") or {}
    status = output.get("status")
    if status and status != "ok":
        print(f"Statement output status: {status}", file=sys.stderr)

    data = output.get("data") or {}
    if "text/plain" in data:
        print(data["text/plain"], end="" if str(data["text/plain"]).endswith("\n") else "\n")
        return
    if "application/json" in data:
        print(json.dumps(data["application/json"], indent=2), flush=True)
        return
    if "ename" in output or "evalue" in output:
        print(f"{output.get('ename', 'Error')}: {output.get('evalue', '')}", file=sys.stderr)
        traceback = output.get("traceback") or []
        for line in traceback:
            print(line, file=sys.stderr)
        raise FabricLivyError("Livy statement returned an error output")
    if output:
        print(json.dumps(output, indent=2), flush=True)


def parse_conf(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise argparse.ArgumentTypeError(f"--conf-json is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise argparse.ArgumentTypeError("--conf-json must be a JSON object")
    return parsed


def parse_args() -> argparse.Namespace:
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
    parser.add_argument("--api-base-url", default=os.environ.get("FABRIC_API_BASE_URL", DEFAULT_API_BASE_URL))
    parser.add_argument("--api-version", default=os.environ.get("FABRIC_LIVY_API_VERSION", DEFAULT_API_VERSION))
    parser.add_argument(
        "--scope",
        default=os.environ.get("FABRIC_API_SCOPE", os.environ.get("FABRIC_TOKEN_SCOPE", DEFAULT_SCOPE)),
    )
    parser.add_argument("--code", default=DEFAULT_CODE)
    parser.add_argument("--file", type=Path, help="Read Python code from a file instead of --code")
    parser.add_argument("--kind", default="pyspark", help="Livy statement kind, usually pyspark")
    parser.add_argument("--session-id", help="Reuse an existing Livy session instead of creating one")
    parser.add_argument("--keep-session", action="store_true", help="Do not delete a newly created session")
    parser.add_argument("--environment-id", default=os.environ.get("FABRIC_ENVIRONMENT_ID"))
    parser.add_argument("--conf-json", type=parse_conf, default={}, help="Additional session conf JSON object")
    parser.add_argument("--poll-interval", type=float, default=5.0)
    parser.add_argument("--timeout", type=float, default=1200.0)
    parser.add_argument("--show-json", action="store_true", help="Print the full final statement JSON")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    code = args.file.read_text(encoding="utf-8") if args.file else args.code
    token = get_access_token(args.scope)
    try:
        workspace_id = args.workspace_id or resolve_workspace_id(
            args.api_base_url,
            token,
            args.workspace_name,
        )
        lakehouse_id = args.lakehouse_id or resolve_lakehouse_id(
            args.api_base_url,
            token,
            workspace_id,
            args.lakehouse_name,
        )
    except Exception as exc:  # noqa: BLE001 - CLI should surface auth/API failures.
        print(f"Spark session failed: {exc}", file=sys.stderr)
        return 1

    sessions_url = livy_sessions_url(
        workspace_id=workspace_id,
        lakehouse_id=lakehouse_id,
        api_base_url=args.api_base_url,
        api_version=args.api_version,
    )
    created_session = False

    try:
        if args.session_id:
            session_id = args.session_id
            session_url = f"{sessions_url}/{session_id}"
        else:
            session = create_session(
                sessions_url=sessions_url,
                token=token,
                environment_id=args.environment_id,
                conf=args.conf_json,
            )
            session_id = str(session["id"])
            session_url = f"{sessions_url}/{session_id}"
            created_session = True
            print(f"created session {session_id}", file=sys.stderr, flush=True)

        wait_for_session_idle(session_url, token, args.poll_interval, args.timeout)
        statement = submit_statement(session_url, token, code, args.kind)
        statement_id = str(statement["id"])
        print(f"submitted statement {statement_id}", file=sys.stderr, flush=True)
        final_statement = wait_for_statement(
            f"{session_url}/statements/{statement_id}",
            token,
            args.poll_interval,
            args.timeout,
        )
        print_statement_output(final_statement, args.show_json)
    except Exception as exc:  # noqa: BLE001 - CLI should surface auth/API/runtime failures.
        print(f"Spark session failed: {exc}", file=sys.stderr)
        return 1
    finally:
        if created_session and not args.keep_session:
            try:
                delete_session(session_url, token)
                print(f"deleted session {session_id}", file=sys.stderr, flush=True)
            except Exception as exc:  # noqa: BLE001 - cleanup should not hide primary output.
                print(f"failed to delete session {session_id}: {exc}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
