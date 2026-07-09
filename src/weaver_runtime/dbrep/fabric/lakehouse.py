"""Fabric Lakehouse build (stage + OneLake upload) and load (Livy Spark)."""

from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path

from ..build.planner import BuildPair
from ..build.runtime_bundle import install_build
from ..errors import LoadError
from . import onelake

_LOAD_CODE = '''
import json, sys
import notebookutils

_ABFSS = "abfss://{workspace_id}@onelake.dfs.fabric.microsoft.com/{lakehouse_id}"
_MOUNT = "/weaver_load_mount"
try:
    notebookutils.fs.mount(_ABFSS, _MOUNT)
except Exception:
    pass
_LOCAL = notebookutils.fs.getMountPath(_MOUNT)
_RT = _LOCAL + "/Files/_weaver/runtime"
sys.path.insert(0, _RT + "/_orchestrator")
from weaver_runtime.dbrep.runtime.orchestrator import load_target_runtime
# Read runtime + write Folder files through the mount (Python IO); read/write
# Delta and Folder outputs through abfss (the mount cannot host Spark writes).
report = load_target_runtime(_RT, execute=True, spark=spark, spark_root=_ABFSS)
print("WEAVER_LOAD_RESULT " + json.dumps(report.to_dict()))
'''


@dataclass
class FabricBuildResult:
    targets: tuple[str, ...]
    workspace: str
    lakehouse: str
    uploaded: int
    runtime_root: str


def build_fabric_lakehouse(plan, fabric_pairs) -> list[FabricBuildResult]:
    """Stage the bundle locally and upload Files/ to each Fabric Lakehouse."""

    by_host: dict[str, list] = {}
    for pair in fabric_pairs:
        by_host.setdefault(pair.target.host, []).append(pair)

    results: list[FabricBuildResult] = []
    for _host, pairs in by_host.items():
        with tempfile.TemporaryDirectory(prefix="weaver_fabric_stage_") as tmp:
            staging = Path(tmp)
            staging_pairs = tuple(
                BuildPair(pair.source, replace(pair.target, host=str(staging), platform="local"))
                for pair in pairs
            )
            staging_plan = replace(plan, pairs=staging_pairs)
            install_build(staging_plan)

            resolved = onelake.resolve_lakehouse(
                pairs[0].target.fabric_workspace, pairs[0].target.fabric_lakehouse
            )
            uploaded = onelake.upload_files_tree(staging / "Files", resolved)
            results.append(
                FabricBuildResult(
                    targets=tuple(pair.target.alias for pair in pairs),
                    workspace=resolved["workspace_name"],
                    lakehouse=resolved["lakehouse_name"],
                    uploaded=uploaded,
                    runtime_root="Files/_weaver/runtime",
                )
            )
    return results


def load_fabric_lakehouse(
    target,
    *,
    poll_interval: float = 10.0,
    timeout: float = 1800.0,
) -> dict:
    """Run the installed runtime in Fabric Spark via a Livy session."""

    from weaver_runtime._legacy import load_script_module

    livy = load_script_module("sparksession")
    resolved = onelake.resolve_lakehouse(target.fabric_workspace, target.fabric_lakehouse)
    token = livy.get_access_token()
    sessions_url = livy.livy_sessions_url(resolved["workspace_id"], resolved["lakehouse_id"])

    code = _LOAD_CODE.format(
        workspace_id=resolved["workspace_id"], lakehouse_id=resolved["lakehouse_id"]
    )
    session = livy.create_session(sessions_url, token)
    session_url = f"{sessions_url}/{session['id']}"
    try:
        livy.wait_for_session_idle(session_url, token, poll_interval, timeout)
        statement = livy.submit_statement(session_url, token, code, "pyspark")
        final = livy.wait_for_statement(
            f"{session_url}/statements/{statement['id']}", token, poll_interval, timeout
        )
    finally:
        try:
            livy.delete_session(session_url, token)
        except Exception:  # pragma: no cover - cleanup best effort
            pass

    output = final.get("output") or {}
    if output.get("status") != "ok":
        raise LoadError(
            f"Fabric load failed: {output.get('ename')}: {output.get('evalue')}"
        )
    report = _parse_report((output.get("data") or {}).get("text/plain", ""))
    return {
        "target": target.alias,
        "type": "Fabric Lakehouse",
        "workspace": resolved["workspace_name"],
        "lakehouse": resolved["lakehouse_name"],
        "executed": True,
        "report": report,
    }


def _parse_report(text: str) -> dict:
    for line in text.splitlines():
        if line.startswith("WEAVER_LOAD_RESULT "):
            return json.loads(line[len("WEAVER_LOAD_RESULT ") :])
    raise LoadError(f"Fabric load did not return a result marker; output was:\n{text}")
