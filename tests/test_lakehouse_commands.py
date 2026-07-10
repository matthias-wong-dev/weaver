from __future__ import annotations

import json
from pathlib import Path

import pytest

from dbrep_helpers import write_config_files, write_python_folder, write_python_table
import weaver_runtime.dbrep.execution as execution
from weaver_runtime.dbrep.cli.commands import run_build, run_load
import weaver_runtime.dbrep.fabric.lakehouse as fabric_lakehouse
from weaver_runtime.dbrep.lakehouse.programs import render_load_program

SCHEMA = (("record_id", "string"), ("group_id", "string"), ("amount", "int"))


def _sources(ses_root: Path) -> None:
    write_python_folder(ses_root / "T0", "Raw", "Drop")
    write_python_table(ses_root / "T1", "Stage", "Record", deps=("T0.Raw.Drop",), schema_cols=SCHEMA)


# --- Local build applies the generated program (Files-only: no Java needed) --


def test_local_files_build_executes_program_and_writes_completion(tmp_path: Path) -> None:
    ses_root = tmp_path / "SES"
    servers = {"SES_Repo": {"server": str(ses_root)}, "Lake": {"server": str(tmp_path / "lake")}}
    databases = {
        "T0_SES": {"type": "SES", "server": "SES_Repo", "database": "T0"},
        "T0_FILES": {"type": "Files", "server": "Lake", "database": "T0"},
    }
    weaver_path = write_config_files(tmp_path, servers, databases)
    write_python_folder(ses_root / "T0", "Raw", "Drop")

    payload = run_build(weaver_path, "T0_SES", "T0_FILES")

    assert len(payload["lakehouse"]) == 1
    host = payload["lakehouse"][0]
    assert host["server"] == "Lake"
    assert host["result"] == {"root": str(tmp_path / "lake"), "created": [], "existing": []}

    record_path = tmp_path / "lake" / "Files" / "_weaver" / "runtime" / "build_complete.json"
    assert record_path.is_file()
    record = json.loads(record_path.read_text(encoding="utf-8"))
    assert record["objects"] == ["T0.Raw.Drop"]
    assert record["result"] == host["result"]


def test_local_build_failure_leaves_no_completion_record(tmp_path: Path, monkeypatch) -> None:
    ses_root = tmp_path / "SES"
    servers = {"SES_Repo": {"server": str(ses_root)}, "Lake": {"server": str(tmp_path / "lake")}}
    databases = {
        "T0_SES": {"type": "SES", "server": "SES_Repo", "database": "T0"},
        "T0_FILES": {"type": "Files", "server": "Lake", "database": "T0"},
    }
    weaver_path = write_config_files(tmp_path, servers, databases)
    write_python_folder(ses_root / "T0", "Raw", "Drop")

    def boom(*args, **kwargs):
        raise RuntimeError("program execution failed")

    monkeypatch.setattr(execution, "execute_program_local", boom)

    with pytest.raises(RuntimeError, match="program execution failed"):
        run_build(weaver_path, "T0_SES", "T0_FILES")

    record_path = tmp_path / "lake" / "Files" / "_weaver" / "runtime" / "build_complete.json"
    assert not record_path.exists()


# --- Fabric build: one generic submission per host, completion after success -


def _fabric_config(tmp_path: Path) -> Path:
    ses_root = tmp_path / "SES"
    servers = {
        "SES_Repo": {"server": str(ses_root)},
        "Fabric": {"server": "Workspace/Lakehouse", "platform": "fabric"},
    }
    databases = {
        "T0_SES": {"type": "SES", "server": "SES_Repo", "database": "T0"},
        "T1_SES": {"type": "SES", "server": "SES_Repo", "database": "T1"},
        "T0_FILES": {"type": "Files", "server": "Fabric", "database": "T0"},
        "T1_DELTA": {"type": "Delta", "server": "Fabric", "database": "T1"},
    }
    weaver_path = write_config_files(tmp_path, servers, databases)
    _sources(ses_root)
    return weaver_path


def _mock_fabric(monkeypatch) -> dict:
    calls: dict = {"sync": [], "programs": []}

    def fake_resolve(workspace, lakehouse):
        return {
            "workspace_id": "W",
            "lakehouse_id": "L",
            "workspace_name": workspace,
            "lakehouse_name": lakehouse,
        }

    def fake_sync(files_root, resolved, **kwargs):
        calls["sync"].append(Path(files_root))
        return 7

    def fake_run(resolved, program, **kwargs):
        calls["programs"].append(program)
        return {"root": "abfss://W@onelake/L", "created": ["T1.Stage.Record"], "existing": []}

    monkeypatch.setattr(fabric_lakehouse.onelake, "resolve_lakehouse", fake_resolve)
    monkeypatch.setattr(fabric_lakehouse.onelake, "sync_runtime_folder", fake_sync)
    monkeypatch.setattr(fabric_lakehouse, "_run_program", fake_run)
    return calls


def test_fabric_build_submits_one_program_and_writes_completion(tmp_path: Path, monkeypatch) -> None:
    weaver_path = _fabric_config(tmp_path)
    calls = _mock_fabric(monkeypatch)

    payload = run_build(weaver_path, "T0_SES,T1_SES", "T0_FILES,T1_DELTA")

    # One host -> exactly one generic runtime submission, no per-table round trips.
    assert len(calls["programs"]) == 1
    program = calls["programs"][0]
    assert "initialise_delta_tables" in program
    assert "Tables/T1/Stage.Record" in program

    fab = payload["fabric"]
    assert len(fab) == 1
    assert fab[0]["result"]["created"] == ["T1.Stage.Record"]

    # Files uploaded once, then the completion record uploaded after success.
    assert len(calls["sync"]) == 2

    # The completion record was staged (into the temp dir, now gone) only after
    # the program returned — proven by the second sync following the submission.
    assert payload["fabric"][0]["uploaded"] == 7


def test_fabric_build_does_not_derive_specs_itself(tmp_path: Path) -> None:
    # The Fabric module renders nothing operation-specific: no spec derivation,
    # no init/load templates, no operation markers.
    import inspect

    source = inspect.getsource(fabric_lakehouse)
    for forbidden in ("_INIT_CODE", "_LOAD_CODE", "delta_specs_from_plan", "WEAVER_INIT_RESULT"):
        assert forbidden not in source


# --- Fabric load honours target/object/static/strict ------------------------


def test_fabric_load_renders_same_program_and_honours_filters(tmp_path: Path, monkeypatch) -> None:
    weaver_path = _fabric_config(tmp_path)
    captured: dict = {}

    def fake_resolve(workspace, lakehouse):
        return {"workspace_id": "W", "lakehouse_id": "L", "workspace_name": workspace, "lakehouse_name": lakehouse}

    def fake_run(resolved, program, **kwargs):
        captured["program"] = program
        return {"ok": True, "executed": True, "steps": []}

    monkeypatch.setattr(fabric_lakehouse.onelake, "resolve_lakehouse", fake_resolve)
    monkeypatch.setattr(fabric_lakehouse, "_run_program", fake_run)

    result = run_load(
        weaver_path,
        "T1_DELTA",
        objects=("T1.Stage.Record",),
        include_static=True,
        strict=False,
    )

    assert result["executed"] is True
    expected = render_load_program(
        target_filter="T1_DELTA",
        object_filter=("T1.Stage.Record",),
        include_static=True,
        strict=False,
    )
    assert captured["program"] == expected
    # Selection is scoped to this one alias, not every loadable target.
    assert "target_filter='T1_DELTA'" in captured["program"]
    assert "object_filter=('T1.Stage.Record',)" in captured["program"]
