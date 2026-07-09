from __future__ import annotations

import json
import shutil
from pathlib import Path

from dbrep_helpers import write_config_files, write_python_folder, write_python_table
from weaver_runtime.cli import main


def _build(tmp_path: Path, capsys) -> Path:
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
    main(["build", "--config", str(weaver_path), "--from", "T0_SES,T1_SES", "--to", "T0_FILES,T1_DELTA"])
    capsys.readouterr()
    return weaver_path


def test_load_is_target_only_and_reads_installed_runtime(tmp_path: Path, capsys) -> None:
    weaver_path = _build(tmp_path, capsys)
    code = main(["load", "--config", str(weaver_path), "--target", "T1_DELTA", "--dry-run"])
    assert code == 0
    report = json.loads(capsys.readouterr().out)
    assert report["target"] == "T1_DELTA"
    assert report["ok"] is True
    step_ids = [step["object_id"] for step in report["steps"]]
    assert step_ids[0] == "T0.Raw.Drop"
    assert "T1.Stage.Record" in step_ids


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
    assert [step["object_id"] for step in report["steps"]] == [
        "T0.Raw.Drop",
        "T1.Stage.Record",
    ]


def test_load_unknown_target_errors(tmp_path: Path, capsys) -> None:
    weaver_path = _build(tmp_path, capsys)
    code = main(["load", "--config", str(weaver_path), "--target", "Nope", "--dry-run"])
    assert code == 1
    assert "unknown database representation" in capsys.readouterr().err
