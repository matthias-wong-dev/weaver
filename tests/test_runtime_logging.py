"""Durable workflow and step logging over a real Folder-only load.

Folder loads need no Spark, so these exercise the full workflow lifecycle —
workflow id, one log directory, per-step JSON, immediate success/failure
persistence, and structured exceptions — in the core suite. The Spark
integration test proves the same contract for Table (row) CRUD.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from dbrep_helpers import load_config, resolve, write_config_files
from weaver_runtime.dbrep.build import BuildPair, BuildRequest, plan_build
from weaver_runtime.dbrep.build.runtime_bundle import install_build
from weaver_runtime.dbrep.errors import LoadError
from weaver_runtime.dbrep.runtime.orchestrator import load_target_runtime

WORKFLOW_DIR_RE = re.compile(r"^\d{8}T\d{6}Z_[0-9a-f]{6}$")
STEP_FILE_RE = re.compile(r"^\d{8}T\d{6}\.\d{6}Z_[0-9a-f]{6}\.json$")

FIRST = '''"""
Folder ID: Raw.First
Description: First folder.
Lineage: Stages the first drop file.
"""
from weaver_runtime.dbrep.objects import Folder


class Raw__First(Folder):
    def read(self):
        with self.staging_folder() as staging:
            (staging.path / "first.csv").write_text("id\\n1\\n", encoding="utf-8")
        return staging, ()
'''

SECOND = '''"""
Folder ID: Raw.Second
Description: Second folder.
Lineage: Stages the second drop file after the first.
"""
from weaver_runtime.dbrep.objects import Folder


class Raw__Second(Folder):
    def read(self):
        _ = self.repo["Raw.First"]
        # Return inside the `with` block: normal exit must preserve staging so
        # Weaver can still consume it (identical to returning after the block).
        with self.staging_folder() as staging:
            (staging.path / "second.csv").write_text("id\\n2\\n", encoding="utf-8")
            return staging, ()
'''

SECOND_FAILS = '''"""
Folder ID: Raw.Second
Description: Second folder.
Lineage: Fails after the first folder has succeeded.
"""
from weaver_runtime.dbrep.objects import Folder


class Raw__Second(Folder):
    def read(self):
        _ = self.repo["Raw.First"]
        raise RuntimeError("second boom")
'''


def _install_files_runtime(tmp_path: Path, objects: dict[str, str]) -> Path:
    ses_root = tmp_path / "SES"
    (ses_root / "T0").mkdir(parents=True)
    for name, source in objects.items():
        (ses_root / "T0" / f"{name}.py").write_text(source, encoding="utf-8")

    servers = {
        "SES_Repo": {"server": str(ses_root)},
        "Lake": {"server": str(tmp_path / "lake")},
    }
    databases = {
        "T0_SES": {"type": "SES", "server": "SES_Repo", "database": "T0"},
        "T0_FILES": {"type": "Files", "server": "Lake", "database": "T0"},
    }
    weaver_path = write_config_files(tmp_path, servers, databases)
    config = load_config(weaver_path)
    plan = plan_build(
        BuildRequest(pairs=(BuildPair(resolve(config, "T0_SES"), resolve(config, "T0_FILES")),))
    )
    install_build(plan)
    return tmp_path / "lake" / "Files" / "_weaver" / "runtime"


def _records_by_object(log_dir: Path) -> dict[str, dict]:
    return {
        record["object_id"]: record
        for record in (json.loads(path.read_text()) for path in log_dir.glob("*.json"))
    }


def test_load_creates_single_workflow_dir_with_one_file_per_step(tmp_path: Path) -> None:
    runtime = _install_files_runtime(tmp_path, {"Raw__First": FIRST, "Raw__Second": SECOND})
    report = load_target_runtime(runtime, execute=True)
    assert report.ok is True and report.executed is True

    logs_root = tmp_path / "lake" / "Files" / "_logs"
    workflow_dirs = list(logs_root.iterdir())
    assert len(workflow_dirs) == 1
    workflow_dir = workflow_dirs[0]
    assert workflow_dir.name == report.workflow_id
    assert WORKFLOW_DIR_RE.match(workflow_dir.name)

    step_files = sorted(workflow_dir.glob("*.json"))
    assert len(step_files) == 2
    for step_file in step_files:
        assert STEP_FILE_RE.match(step_file.name)


def test_names_live_in_json_not_filenames_and_share_workflow_id(tmp_path: Path) -> None:
    runtime = _install_files_runtime(tmp_path, {"Raw__First": FIRST, "Raw__Second": SECOND})
    report = load_target_runtime(runtime, execute=True)
    log_dir = Path(report.log_dir)

    records = _records_by_object(log_dir)
    assert set(records) == {"T0.Raw.First", "T0.Raw.Second"}
    assert {record["workflow_id"] for record in records.values()} == {report.workflow_id}
    assert records["T0.Raw.First"]["module"] == "Raw__First.py"
    assert records["T0.Raw.Second"]["module"] == "Raw__Second.py"

    for step_file in log_dir.glob("*.json"):
        for token in ("Raw", "First", "Second"):
            assert token not in step_file.name


def test_folder_logs_use_unit_files_without_custom_messages(tmp_path: Path) -> None:
    runtime = _install_files_runtime(tmp_path, {"Raw__First": FIRST, "Raw__Second": SECOND})
    report = load_target_runtime(runtime, execute=True)

    steps = {step.object_id: step for step in report.steps}
    first = steps["T0.Raw.First"]
    assert first.kind == "Folder"
    assert first.status == "success"
    assert first.crud.unit == "files"
    assert (first.crud.read, first.crud.created) == (1, 1)
    record = _records_by_object(Path(report.log_dir))["T0.Raw.First"]
    assert record["crud"]["unit"] == "files"
    assert "messages" not in record


def test_table_kind_maps_to_rows_unit() -> None:
    from weaver_runtime.dbrep.runtime.logging import crud_unit_for_kind, planned_step_log

    assert crud_unit_for_kind("Folder") == "files"
    assert crud_unit_for_kind("Table") == "rows"
    assert planned_step_log("T1.Stage.Record", "Table").to_dict()["crud"]["unit"] == "rows"


def test_failed_step_is_logged_and_earlier_logs_remain(tmp_path: Path) -> None:
    runtime = _install_files_runtime(
        tmp_path, {"Raw__First": FIRST, "Raw__Second": SECOND_FAILS}
    )
    with pytest.raises(LoadError):
        load_target_runtime(runtime, execute=True)

    logs_root = tmp_path / "lake" / "Files" / "_logs"
    workflow_dirs = list(logs_root.iterdir())
    assert len(workflow_dirs) == 1

    records = _records_by_object(workflow_dirs[0])
    # The earlier successful step survives the later failure.
    assert records["T0.Raw.First"]["status"] == "success"

    failed = records["T0.Raw.Second"]
    assert failed["status"] == "failed"
    assert failed["completed_at"] is not None
    error = failed["error"]
    assert error["type"].endswith("RuntimeError")
    assert "second boom" in error["message"]
    assert isinstance(error["traceback"], list) and error["traceback"]
    # A failed step still carries a CRUD block (zeros at failure time).
    assert failed["crud"] == {
        "unit": "files",
        "read": 0,
        "created": 0,
        "updated": 0,
        "deleted": 0,
    }


def test_load_report_includes_workflow_id_and_log_dir(tmp_path: Path) -> None:
    runtime = _install_files_runtime(tmp_path, {"Raw__First": FIRST})
    report = load_target_runtime(runtime, execute=True)

    data = report.to_dict()
    assert data["workflow_id"] == report.workflow_id
    assert data["log_dir"] == report.log_dir
    assert data["log_dir"].endswith(report.workflow_id)
    assert Path(data["log_dir"]).is_dir()
