from __future__ import annotations

import json
import shutil
from pathlib import Path

from dbrep_helpers import (
    load_config,
    resolve,
    write_config_files,
    write_python_folder,
    write_python_table,
)
from weaver_runtime.cli import main
from weaver_runtime.dbrep.build import BuildPair, BuildRequest, plan_build
from weaver_runtime.dbrep.build.runtime_bundle import install_build


def _build(tmp_path: Path, capsys) -> Path:
    # Install the runtime bundle directly. These are load tests: they only need
    # the installed catalogue to select and order steps, not a Spark Delta build.
    ses_root = tmp_path / "SES"
    servers = {
        "SES_Repo": {"server": str(ses_root)},
        "Lake": {"server": str(tmp_path / "lake")},
    }
    databases = {
        "T0_SES": {"type": "SES", "server": "SES_Repo", "database": "T0"},
        "T1_SES": {"type": "SES", "server": "SES_Repo", "database": "T1"},
        "T0_FILES": {"type": "Files", "server": "Lake", "database": "T0"},
        "T1_DELTA": {"type": "Delta", "server": "Lake", "database": "T1"},
    }
    weaver_path = write_config_files(tmp_path, servers, databases)
    write_python_folder(ses_root / "T0", "Raw", "Drop")
    write_python_table(ses_root / "T1", "Stage", "Record", deps=("T0.Raw.Drop",))
    write_python_table(ses_root / "T1", "Mart", "RecordCurrent", deps=("Stage.Record",))

    config = load_config(weaver_path)
    plan = plan_build(
        BuildRequest(
            pairs=(
                BuildPair(resolve(config, "T0_SES"), resolve(config, "T0_FILES")),
                BuildPair(resolve(config, "T1_SES"), resolve(config, "T1_DELTA")),
            )
        )
    )
    install_build(plan)
    return weaver_path


def test_load_is_target_only_and_reads_installed_runtime(tmp_path: Path, capsys) -> None:
    weaver_path = _build(tmp_path, capsys)
    code = main(["load", "--config", str(weaver_path), "--target", "T1_DELTA", "--dry-run"])
    assert code == 0
    report = json.loads(capsys.readouterr().out)
    assert report["target"] == "T1_DELTA"
    assert report["ok"] is True
    step_ids = [step["object_id"] for step in report["steps"]]
    assert step_ids == ["T1.Stage.Record", "T1.Mart.RecordCurrent"]


def test_load_files_target_excludes_delta_steps(tmp_path: Path, capsys) -> None:
    weaver_path = _build(tmp_path, capsys)
    code = main(["load", "--config", str(weaver_path), "--target", "T0_FILES", "--dry-run"])
    assert code == 0
    report = json.loads(capsys.readouterr().out)
    assert [step["object_id"] for step in report["steps"]] == ["T0.Raw.Drop"]


def test_load_does_not_read_source_repo(tmp_path: Path, capsys) -> None:
    weaver_path = _build(tmp_path, capsys)
    # Delete the source SES repo entirely; load must still work from the runtime.
    shutil.rmtree(tmp_path / "SES")
    code = main(["load", "--config", str(weaver_path), "--target", "T1_DELTA", "--dry-run"])
    assert code == 0
    report = json.loads(capsys.readouterr().out)
    assert report["ok"] is True
    assert report["executed"] is False


def test_load_object_filter(tmp_path: Path, capsys) -> None:
    weaver_path = _build(tmp_path, capsys)
    code = main(
        [
            "load",
            "--config",
            str(weaver_path),
            "--target",
            "T1_DELTA",
            "--dry-run",
            "--object",
            "T1.Stage.Record",
        ]
    )
    assert code == 0
    report = json.loads(capsys.readouterr().out)
    assert [step["object_id"] for step in report["steps"]] == ["T1.Stage.Record"]


def test_local_files_load_executes_and_writes_workflow_log(tmp_path: Path, capsys) -> None:
    # A real (non-dry-run) local Files load runs the exact generated program the
    # Fabric path submits — no Spark needed for a Folder-only target — and must
    # return the workflow fields and write one durable log directory on disk.
    weaver_path = _build(tmp_path, capsys)
    capsys.readouterr()
    code = main(["load", "--config", str(weaver_path), "--target", "T0_FILES"])
    assert code == 0

    report = json.loads(capsys.readouterr().out)
    assert report["ok"] is True
    assert report["executed"] is True
    assert report["workflow_id"]
    assert report["log_dir"].endswith(report["workflow_id"])

    steps = {step["object_id"]: step for step in report["steps"]}
    assert steps["T0.Raw.Drop"]["status"] == "success"
    assert steps["T0.Raw.Drop"]["kind"] == "Folder"
    assert steps["T0.Raw.Drop"]["crud"]["unit"] == "files"

    assert (tmp_path / "lake" / "Files" / "_logs" / report["workflow_id"]).is_dir()


def test_load_unknown_target_errors(tmp_path: Path, capsys) -> None:
    weaver_path = _build(tmp_path, capsys)
    code = main(["load", "--config", str(weaver_path), "--target", "Nope", "--dry-run"])
    assert code == 1
    assert "unknown database representation" in capsys.readouterr().err
