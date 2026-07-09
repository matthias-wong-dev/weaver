from __future__ import annotations

import json
from pathlib import Path

import pytest

from dbrep_helpers import write_config_files, write_python_folder, write_python_table
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


def test_plan_subcommand(tmp_path: Path, capsys) -> None:
    weaver_path = _setup(tmp_path)
    code = main(
        ["plan", "--config", str(weaver_path), "--from", "T0_SES,T1_SES", "--to", "T0_FILES,T1_DELTA"]
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["dry_run"] is True
    assert payload["objects"][0] == "T0.Raw.Drop"


def test_discover_subcommand(tmp_path: Path, capsys) -> None:
    weaver_path = _setup(tmp_path)
    code = main(["discover", "--config", str(weaver_path), "--database", "T1_SES"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    ids = {entry["id"] for entry in payload["objects"]}
    assert ids == {"T1.Stage.Record"}
    # T0.Raw.Drop is cross-database and unsupplied here, so it is external.
    stage = payload["objects"][0]
    assert stage["dependencies"][0]["scope"] == "external"


def test_manifest_subcommand_after_build(tmp_path: Path, capsys) -> None:
    weaver_path = _setup(tmp_path)
    main(["build", "--config", str(weaver_path), "--from", "T0_SES,T1_SES", "--to", "T0_FILES,T1_DELTA"])
    capsys.readouterr()
    code = main(["manifest", "--config", str(weaver_path), "--target", "T1_DELTA"])
    assert code == 0
    manifest = json.loads(capsys.readouterr().out)
    assert manifest["object_count"] == 2
    assert "objects" not in manifest


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
