"""Fabric Spark Livy session lifecycle: create, wait, submit, result, delete."""

from __future__ import annotations

import time
from typing import Any

from .client import FabricClientError, request_json
from .settings import DEFAULT_API_BASE_URL, DEFAULT_LIVY_API_VERSION

TERMINAL_SESSION_STATES = {"dead", "error", "killed", "cancelled", "canceled"}
TERMINAL_STATEMENT_STATES = {"error", "cancelled", "canceled"}


class LivyError(FabricClientError):
    """Raised when a Fabric Livy request fails."""


def sessions_url(
    workspace_id: str,
    lakehouse_id: str,
    api_base_url: str = DEFAULT_API_BASE_URL,
    api_version: str = DEFAULT_LIVY_API_VERSION,
) -> str:
    return (
        f"{api_base_url.rstrip('/')}/v1"
        f"/workspaces/{workspace_id}"
        f"/lakehouses/{lakehouse_id}"
        f"/livyapi/versions/{api_version}"
        "/sessions"
    )


def _json(method: str, url: str, token: str, payload=None, expected_statuses=None) -> dict[str, Any]:
    result, _, _ = request_json(method, url, token, payload, expected_statuses or {200})
    return result or {}


def create_session(
    url: str,
    token: str,
    *,
    environment_id: str | None = None,
    conf: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    session_conf = dict(conf or {})
    if environment_id:
        import json

        session_conf["spark.fabric.environmentDetails"] = json.dumps({"id": environment_id})
    if session_conf:
        payload["conf"] = session_conf
    return _json("POST", url, token, payload, {200, 201, 202})


def wait_for_idle(session_url: str, token: str, poll_interval: float, timeout: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while True:
        session = _json("GET", session_url, token)
        state = str(session.get("state", "")).lower()
        if state == "idle":
            return session
        if state in TERMINAL_SESSION_STATES:
            raise LivyError(f"Livy session entered terminal state {state!r}: {session}")
        if time.monotonic() >= deadline:
            raise LivyError(f"timed out waiting for Livy session to become idle: {session}")
        time.sleep(poll_interval)


def submit_statement(session_url: str, token: str, code: str, kind: str = "pyspark") -> dict[str, Any]:
    return _json(
        "POST",
        f"{session_url}/statements",
        token,
        {"code": code, "kind": kind},
        {200, 201, 202},
    )


def wait_for_statement(statement_url: str, token: str, poll_interval: float, timeout: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while True:
        statement = _json("GET", statement_url, token)
        state = str(statement.get("state", "")).lower()
        if state == "available":
            return statement
        if state in TERMINAL_STATEMENT_STATES:
            raise LivyError(f"Livy statement entered terminal state {state!r}: {statement}")
        if time.monotonic() >= deadline:
            raise LivyError(f"timed out waiting for Livy statement: {statement}")
        time.sleep(poll_interval)


def delete_session(session_url: str, token: str) -> None:
    _json("DELETE", session_url, token, expected_statuses={200, 202, 204})


def statement_result(statement: dict[str, Any]) -> dict[str, Any]:
    """Return the output of a finished statement, raising on error status."""

    output = statement.get("output") or {}
    if output.get("status") and output.get("status") != "ok":
        raise LivyError(
            f"Livy statement failed: {output.get('ename')}: {output.get('evalue')}"
        )
    return output


def run_code(
    workspace_id: str,
    lakehouse_id: str,
    token: str,
    code: str,
    *,
    kind: str = "pyspark",
    api_base_url: str = DEFAULT_API_BASE_URL,
    api_version: str = DEFAULT_LIVY_API_VERSION,
    poll_interval: float = 5.0,
    timeout: float = 1200.0,
    environment_id: str | None = None,
    conf: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a session, run one statement, and return its output; always cleans up."""

    base = sessions_url(workspace_id, lakehouse_id, api_base_url, api_version)
    session = create_session(base, token, environment_id=environment_id, conf=conf)
    session_url = f"{base}/{session['id']}"
    try:
        wait_for_idle(session_url, token, poll_interval, timeout)
        statement = submit_statement(session_url, token, code, kind)
        final = wait_for_statement(
            f"{session_url}/statements/{statement['id']}", token, poll_interval, timeout
        )
    finally:
        try:
            delete_session(session_url, token)
        except Exception:  # pragma: no cover - cleanup best effort
            pass
    return statement_result(final)
