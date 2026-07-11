from __future__ import annotations

from pathlib import Path

import pytest

from dbrep_helpers import make_config, resolve
from weaver_runtime.dbrep.cli.commands import run_wipe
from weaver_runtime.dbrep.errors import LoadError


def _config(tmp_path: Path):
    servers = {"Lake": {"server": str(tmp_path / "lake")}}
    databases = {
        "T0_FILES": {"type": "Files", "server": "Lake", "database": "T0"},
        "T1_DELTA": {"type": "Delta", "server": "Lake", "database": "T1"},
        "SES": {"type": "SES", "server": "Lake", "database": "src"},
    }
    return make_config(tmp_path, servers, databases)


def _write_config_file(tmp_path: Path) -> Path:
    import yaml

    (tmp_path / "env.yml").write_text(
        yaml.safe_dump({"version": 1, "servers": {"Lake": {"server": str(tmp_path / "lake")}}}),
        encoding="utf-8",
    )
    weaver = {
        "version": 1,
        "uses": {"environment": "env.yml"},
        "databases": {
            "T0_FILES": {"type": "Files", "server": "Lake", "database": "T0"},
            "T1_DELTA": {"type": "Delta", "server": "Lake", "database": "T1"},
            "SES": {"type": "SES", "server": "Lake", "database": "src"},
        },
    }
    path = tmp_path / "weaver.yml"
    path.write_text(yaml.safe_dump(weaver), encoding="utf-8")
    return path


def _materialise(lake: Path) -> None:
    # Files materialisation + a runtime bundle that must survive a Files wipe.
    (lake / "Files" / "T0" / "Raw" / "Drop").mkdir(parents=True)
    (lake / "Files" / "T0" / "Raw" / "Drop" / "_weaver.json").write_text("{}", encoding="utf-8")
    (lake / "Files" / "_weaver" / "runtime").mkdir(parents=True)
    (lake / "Files" / "_weaver" / "runtime" / "catalogue.json").write_text("{}", encoding="utf-8")
    # Delta materialisation (schema and object are separate path components).
    (lake / "Tables" / "T1" / "Stage" / "Record" / "_delta_log").mkdir(parents=True)
    (lake / "Tables" / "T1" / "Stage" / "Record" / "part-0.parquet").write_text("x", encoding="utf-8")


def test_wipe_files_target_deletes_folder_but_not_runtime(tmp_path: Path) -> None:
    lake = tmp_path / "lake"
    _materialise(lake)
    weaver = _write_config_file(tmp_path)

    result = run_wipe(weaver, "T0_FILES")

    assert result["type"] == "Files"
    assert result["platform"] == "local"
    assert result["existed"] is True
    assert not (lake / "Files" / "T0").exists()
    # The runtime bundle under Files/_weaver is untouched.
    assert (lake / "Files" / "_weaver" / "runtime" / "catalogue.json").is_file()
    # Delta tables untouched by a Files wipe.
    assert (lake / "Tables" / "T1" / "Stage" / "Record").exists()


def test_wipe_delta_target_deletes_tables(tmp_path: Path) -> None:
    lake = tmp_path / "lake"
    _materialise(lake)
    weaver = _write_config_file(tmp_path)

    result = run_wipe(weaver, "T1_DELTA")

    assert result["type"] == "Delta"
    assert not (lake / "Tables" / "T1").exists()
    # Files untouched by a Delta wipe.
    assert (lake / "Files" / "T0" / "Raw" / "Drop").exists()


def test_wipe_absent_target_is_noop(tmp_path: Path) -> None:
    (tmp_path / "lake").mkdir()
    weaver = _write_config_file(tmp_path)
    result = run_wipe(weaver, "T0_FILES")
    assert result["existed"] is False


def test_wipe_rejects_ses_target(tmp_path: Path) -> None:
    (tmp_path / "lake").mkdir()
    weaver = _write_config_file(tmp_path)
    with pytest.raises(LoadError, match="does not support target type"):
        run_wipe(weaver, "SES")
