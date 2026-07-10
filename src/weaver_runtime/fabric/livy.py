"""Fabric Spark Livy session lifecycle: create, wait, submit, result, delete."""

from __future__ import annotations

import json
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


# --- Generic Weaver runtime execution ---------------------------------------

RUNTIME_RESULT_MARKER = "WEAVER_RUNTIME_RESULT "

# Generic bootstrap: establish the Fabric runtime environment (mount + standard
# roots) and execute an arbitrary generated Weaver program verbatim. It must not
# name any operation (Delta init, load orchestration, specs, filters, reports).
_RUNTIME_BOOTSTRAP = '''\
import json, sys
import notebookutils

_ABFSS = "abfss://{workspace_id}@onelake.dfs.fabric.microsoft.com/{lakehouse_id}"
_MOUNT = "{mount}"
try:
    notebookutils.fs.mount(_ABFSS, _MOUNT)
except Exception:
    pass
_LOCAL = notebookutils.fs.getMountPath(_MOUNT)
WEAVER_RUNTIME_ROOT = _LOCAL + "/Files/_weaver/runtime"
WEAVER_SPARK_ROOT = _ABFSS
sys.path.insert(0, WEAVER_RUNTIME_ROOT + "/_orchestrator")
_scope = {{
    "spark": spark,
    "WEAVER_RUNTIME_ROOT": WEAVER_RUNTIME_ROOT,
    "WEAVER_SPARK_ROOT": WEAVER_SPARK_ROOT,
}}
exec(compile({program!r}, "<weaver-program>", "exec"), _scope)
if "WEAVER_RESULT" not in _scope:
    raise RuntimeError("generated program did not set WEAVER_RESULT")
print("{marker}" + json.dumps(_scope["WEAVER_RESULT"]))
'''


def run_runtime_program(
    workspace_id: str,
    lakehouse_id: str,
    token: str,
    program: str,
    *,
    api_base_url: str = DEFAULT_API_BASE_URL,
    api_version: str = DEFAULT_LIVY_API_VERSION,
    poll_interval: float = 10.0,
    timeout: float = 1800.0,
    mount: str = "/weaver_runtime_mount",
) -> dict[str, Any]:
    """Execute an arbitrary generated Weaver program in Fabric Spark via Livy.

    Wraps ``program`` in a generic runtime bootstrap (Lakehouse mount + standard
    globals), runs it through :func:`run_code`, and returns the program's
    ``WEAVER_RESULT``. The Livy layer stays operation-agnostic.
    """

    code = _RUNTIME_BOOTSTRAP.format(
        workspace_id=workspace_id,
        lakehouse_id=lakehouse_id,
        mount=mount,
        program=program,
        marker=RUNTIME_RESULT_MARKER,
    )
    output = run_code(
        workspace_id,
        lakehouse_id,
        token,
        code,
        kind="pyspark",
        api_base_url=api_base_url,
        api_version=api_version,
        poll_interval=poll_interval,
        timeout=timeout,
    )
    return parse_runtime_result((output.get("data") or {}).get("text/plain", ""))


def parse_runtime_result(text: str) -> dict[str, Any]:
    for line in text.splitlines():
        if line.startswith(RUNTIME_RESULT_MARKER):
            return json.loads(line[len(RUNTIME_RESULT_MARKER):])
    raise LivyError(
        f"Weaver runtime program did not return a {RUNTIME_RESULT_MARKER.strip()} "
        f"marker; output was:\n{text}"
    )
