from __future__ import annotations

import json
from pathlib import Path

import pytest

from dbrep_helpers import (
    write_config_files,
    write_python_folder,
    write_python_table,
    write_sql_table,
)
from weaver_runtime.cli import main


def _setup(tmp_path: Path) -> Path:
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
    return weaver_path


def test_build_installs_bundle(tmp_path: Path, capsys) -> None:
    weaver_path = _setup(tmp_path)
    code = main(
        [
            "build",
            "--config",
            str(weaver_path),
            "--from",
            "T0_SES,T1_SES",
            "--to",
            "T0_FILES,T1_DELTA",
        ]
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["built"] == ["T0.Raw.Drop", "T1.Stage.Record"]
    assert (tmp_path / "lake" / "Files" / "_weaver" / "runtime" / "manifest.json").is_file()
    assert (tmp_path / "lake" / "Files" / "_weaver" / "runtime" / "catalogue.json").is_file()
    assert (tmp_path / "lake" / "Files" / "T0" / "Raw" / "Drop").is_dir()


def test_build_dry_run_installs_nothing(tmp_path: Path, capsys) -> None:
    weaver_path = _setup(tmp_path)
    code = main(
        [
            "build",
            "--config",
            str(weaver_path),
            "--from",
            "T0_SES,T1_SES",
            "--to",
            "T0_FILES,T1_DELTA",
            "--dry-run",
        ]
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["dry_run"] is True
    assert "T0.Raw.Drop" in payload["objects"]
    assert not (tmp_path / "lake" / "Files" / "_weaver").exists()


def test_build_rejects_unequal_from_to(tmp_path: Path, capsys) -> None:
    weaver_path = _setup(tmp_path)
    code = main(
        ["build", "--config", str(weaver_path), "--from", "T0_SES,T1_SES", "--to", "T0_FILES"]
    )
    assert code == 1
    assert "aliases" in capsys.readouterr().err


def test_generate_stages_bundle_without_installing(tmp_path: Path, capsys) -> None:
    weaver_path = _setup(tmp_path)
    out = tmp_path / "generated"
    code = main(
        [
            "generate",
            "--config",
            str(weaver_path),
            "--from",
            "T0_SES,T1_SES",
            "--to",
            "T0_FILES,T1_DELTA",
            "--out",
            str(out),
        ]
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["generated"] is True
    assert payload["objects"] == ["T0.Raw.Drop", "T1.Stage.Record"]
    # Artifacts staged under --out; the configured Lakehouse target is untouched.
    assert (out / "Lake" / "Files" / "_weaver" / "runtime" / "manifest.json").is_file()
    assert not (tmp_path / "lake" / "Files" / "_weaver").exists()


def test_generate_sql_target_emits_scripts_and_plan(tmp_path: Path, capsys) -> None:
    ses_root = tmp_path / "SES"
    servers = {
        "SES_Repo": {"server": str(ses_root)},
        "Warehouse": {"server": "warehouse.example.test"},
    }
    databases = {
        "T0_SES": {"type": "SES", "server": "SES_Repo", "database": "T0"},
        "T0_WH": {"type": "SQL", "server": "Warehouse", "database": "T0"},
    }
    weaver_path = write_config_files(tmp_path, servers, databases)
    write_sql_table(ses_root / "T0", "Ref", "Thing", query="select 1 as record_id")
    out = tmp_path / "gen"

    code = main(
        [
            "generate",
            "--config", str(weaver_path),
            "--from", "T0_SES",
            "--to", "T0_WH",
            "--out", str(out),
        ]
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["generated"] is True
    assert payload["sql"][0]["target"] == "T0_WH"
    assert (out / "T0_WH" / "plan.json").is_file()
    assert (out / "T0_WH" / "objects" / "T0.Ref.Thing.sql").is_file()


def test_removed_commands_are_not_registered(tmp_path: Path) -> None:
    weaver_path = _setup(tmp_path)
    for removed in ("plan", "discover", "manifest"):
        with pytest.raises(SystemExit):
            main([removed, "--config", str(weaver_path), "--from", "T0_SES", "--to", "T0_FILES"])


def test_prune_removes_orphaned_files_object(tmp_path: Path, capsys) -> None:
    ses_root = tmp_path / "SES"
    servers = {"SES_Repo": {"server": str(ses_root)}, "Lake": {"server": str(tmp_path / "lake")}}
    databases = {
        "T0_SES": {"type": "SES", "server": "SES_Repo", "database": "T0"},
        "T0_FILES": {"type": "Files", "server": "Lake", "database": "T0"},
    }
    weaver_path = write_config_files(tmp_path, servers, databases)
    write_python_folder(ses_root / "T0", "Raw", "Drop")
    write_python_folder(ses_root / "T0", "Raw", "Old")

    main(["build", "--config", str(weaver_path), "--from", "T0_SES", "--to", "T0_FILES"])
    capsys.readouterr()
    assert (tmp_path / "lake" / "Files" / "T0" / "Raw" / "Old").is_dir()

    # Remove one object and rebuild with --prune.
    (ses_root / "T0" / "Raw__Old.py").unlink()
    code = main(["build", "--config", str(weaver_path), "--from", "T0_SES", "--to", "T0_FILES", "--prune"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["pruned"] == ["T0.Raw.Old"]
    assert not (tmp_path / "lake" / "Files" / "T0" / "Raw" / "Old").exists()
    assert (tmp_path / "lake" / "Files" / "T0" / "Raw" / "Drop").is_dir()
