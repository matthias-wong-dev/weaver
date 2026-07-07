#!/usr/bin/env python3
"""Start a deployed Microsoft Fabric notebook through the Job Scheduler API."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urljoin
from urllib.request import Request, urlopen

from azure.identity import DefaultAzureCredential


API_BASE_URL = "https://api.fabric.microsoft.com"
FABRIC_SCOPE = "https://analysis.windows.net/powerbi/api/.default"
DEFAULT_WORKSPACE_NAME = "I Love Government"
TERMINAL_STATES = {"completed", "failed", "cancelled", "canceled", "deduped"}
SUCCESS_STATES = {"completed", "deduped"}


class FabricJobError(RuntimeError):
    """Raised when a Fabric API request or remote job fails."""


def get_access_token(credential: DefaultAzureCredential, scope: str) -> tuple[str, float]:
    """Return a Fabric access token and its epoch expiry time."""

    access_token = credential.get_token(scope)
    return access_token.token, float(access_token.expires_on)


def request(
    method: str,
    url: str,
    token: str,
    payload: dict[str, Any] | None = None,
    expected_statuses: set[int] | None = None,
) -> tuple[dict[str, Any], dict[str, str], int]:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    req = Request(url, data=body, headers=headers, method=method)
    try:
        with urlopen(req, timeout=120) as response:
            response_body = response.read().decode("utf-8")
            if expected_statuses and response.status not in expected_statuses:
                raise FabricJobError(
                    f"{method} {url} returned HTTP {response.status}: {response_body}"
                )
            return (
                json.loads(response_body) if response_body else {},
                {key.lower(): value for key, value in response.headers.items()},
                response.status,
            )
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise FabricJobError(f"{method} {url} returned HTTP {exc.code}: {detail}") from exc
    except URLError as exc:
        raise FabricJobError(f"{method} {url} failed: {exc.reason}") from exc


def paged_values(url: str, token: str) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    while url:
        payload, _, _ = request("GET", url, token, expected_statuses={200})
        values.extend(payload.get("value", []))
        url = payload.get("continuationUri") or payload.get("nextLink") or ""
    return values


def resolve_workspace_id(api_base_url: str, token: str, workspace_name: str) -> str:
    workspaces = paged_values(f"{api_base_url.rstrip('/')}/v1/workspaces", token)
    matches = [workspace for workspace in workspaces if workspace.get("displayName") == workspace_name]
    if len(matches) != 1:
        raise FabricJobError(f"expected one workspace named {workspace_name!r}, found {len(matches)}")
    return str(matches[0]["id"])


def resolve_notebook_id(
    api_base_url: str,
    token: str,
    workspace_id: str,
    notebook_name: str,
) -> str:
    items_url = f"{api_base_url.rstrip('/')}/v1/workspaces/{quote(workspace_id)}/items"
    items = paged_values(items_url, token)
    matches = [
        item
        for item in items
        if item.get("type") == "Notebook" and item.get("displayName") == notebook_name
    ]
    if len(matches) != 1:
        available = sorted(
            str(item.get("displayName")) for item in items if item.get("type") == "Notebook"
        )
        raise FabricJobError(
            f"expected one notebook named {notebook_name!r}, found {len(matches)}; "
            f"available notebooks: {available}"
        )
    return str(matches[0]["id"])


def parse_parameter(raw: str) -> tuple[str, str]:
    if "=" not in raw:
        raise argparse.ArgumentTypeError("parameters must use NAME=VALUE")
    name, value = raw.split("=", 1)
    name = name.strip()
    if not name:
        raise argparse.ArgumentTypeError("parameter name cannot be empty")
    return name, value


def build_job_payload(args: argparse.Namespace) -> dict[str, Any] | None:
    parameters: dict[str, dict[str, str]] = {}

    for name, value in args.parameter:
        parameters[name] = {"value": value, "type": "string"}

    if not parameters:
        return None

    return {"executionData": {"parameters": parameters}}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("notebook_name", nargs="?", help="Deployed Fabric notebook display name")
    parser.add_argument("--notebook", dest="notebook_name_option", help="Deployed Fabric notebook display name")
    parser.add_argument("--notebook-id")
    parser.add_argument("--job-url", help="Existing Fabric notebook job instance URL to wait for.")
    parser.add_argument("--workspace-id", default=os.environ.get("FABRIC_WORKSPACE_ID"))
    parser.add_argument(
        "--workspace-name",
        default=os.environ.get("FABRIC_WORKSPACE_NAME", DEFAULT_WORKSPACE_NAME),
    )
    parser.add_argument("--job-type", default="RunNotebook")
    parser.add_argument("--api-base-url", default=os.environ.get("FABRIC_API_BASE_URL", API_BASE_URL))
    parser.add_argument("--scope", default=os.environ.get("FABRIC_API_SCOPE", FABRIC_SCOPE))
    parser.add_argument("--poll-interval", type=float, default=10.0)
    parser.add_argument("--timeout", type=float, default=7200.0)
    parser.add_argument(
        "--token-refresh-margin",
        type=float,
        default=300.0,
        help="Refresh the Fabric access token this many seconds before expiry while polling.",
    )
    parser.add_argument("--no-wait", action="store_true")
    parser.add_argument(
        "--parameter",
        action="append",
        default=[],
        type=parse_parameter,
        metavar="NAME=VALUE",
        help=(
            "Notebook parameter to pass through Fabric executionData. "
            "Repeat for multiple parameters."
        ),
    )
    args = parser.parse_args()
    if args.notebook_name and args.notebook_name_option:
        parser.error("provide notebook_name or --notebook, not both")
    args.notebook_name = args.notebook_name or args.notebook_name_option
    return args


def main() -> int:
    args = parse_args()
    if not args.job_url and not args.notebook_id and not args.notebook_name:
        print("provide notebook_name or --notebook-id", file=sys.stderr)
        return 2

    credential = DefaultAzureCredential()
    token, token_expires_on = get_access_token(credential, args.scope)

    def refresh_token() -> None:
        nonlocal token, token_expires_on
        token, token_expires_on = get_access_token(credential, args.scope)

    def fabric_request(
        method: str,
        url: str,
        payload: dict[str, Any] | None = None,
        expected_statuses: set[int] | None = None,
    ) -> tuple[dict[str, Any], dict[str, str], int]:
        if time.time() >= token_expires_on - args.token_refresh_margin:
            refresh_token()
        try:
            return request(method, url, token, payload=payload, expected_statuses=expected_statuses)
        except FabricJobError as exc:
            detail = str(exc)
            if "HTTP 401" not in detail or "TokenExpired" not in detail:
                raise
            refresh_token()
            return request(method, url, token, payload=payload, expected_statuses=expected_statuses)

    job_payload = build_job_payload(args)
    if args.job_url:
        workspace_id = args.workspace_id
        notebook_id = args.notebook_id
        job_url = args.job_url
        result = {
            "workspace_id": workspace_id,
            "notebook_id": notebook_id,
            "notebook_name": args.notebook_name,
            "job_url": job_url,
            "status": "Waiting",
            "parameters": {},
        }
    else:
        workspace_id = args.workspace_id or resolve_workspace_id(
            args.api_base_url, token, args.workspace_name
        )
        notebook_id = args.notebook_id or resolve_notebook_id(
            args.api_base_url, token, workspace_id, args.notebook_name
        )
        query = urlencode({"jobType": args.job_type})
        start_url = (
            f"{args.api_base_url.rstrip('/')}/v1/workspaces/{quote(workspace_id)}"
            f"/items/{quote(notebook_id)}/jobs/instances?{query}"
        )
        payload, headers, _ = fabric_request(
            "POST",
            start_url,
            payload=job_payload,
            expected_statuses={202},
        )
        location = headers.get("location")
        if location:
            job_url = urljoin(args.api_base_url, location)
        else:
            job_id = payload.get("id") or payload.get("jobInstanceId")
            if not job_id:
                raise FabricJobError(f"job accepted without a Location header or instance ID: {payload}")
            job_url = f"{start_url.split('?', 1)[0]}/{quote(str(job_id))}"

        result = {
            "workspace_id": workspace_id,
            "notebook_id": notebook_id,
            "notebook_name": args.notebook_name,
            "job_url": job_url,
            "status": "Accepted",
            "parameters": job_payload.get("executionData", {}).get("parameters", {})
            if job_payload
            else {},
        }
    print(json.dumps(result, indent=2), flush=True)
    if args.no_wait:
        return 0

    deadline = time.monotonic() + args.timeout
    last_state = ""
    while True:
        job, _, _ = fabric_request("GET", job_url, expected_statuses={200})
        state = str(job.get("status") or job.get("state") or "unknown")
        if state != last_state:
            print(f"job state: {state}", file=sys.stderr, flush=True)
            last_state = state
        normalized = state.lower()
        if normalized in TERMINAL_STATES:
            print(json.dumps(job, indent=2), flush=True)
            if normalized not in SUCCESS_STATES:
                raise FabricJobError(f"remote notebook job finished with state {state!r}")
            return 0
        if time.monotonic() >= deadline:
            raise FabricJobError(f"timed out waiting for notebook job: {job}")
        time.sleep(args.poll_interval)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except FabricJobError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
