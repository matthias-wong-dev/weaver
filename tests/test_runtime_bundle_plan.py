from __future__ import annotations

import json
from pathlib import Path

from dbrep_helpers import make_config, resolve, write_python_folder, write_python_table
from weaver_runtime.dbrep.build import BuildPair, BuildRequest, plan_build
from weaver_runtime.dbrep.build.runtime_bundle import install_build
from weaver_runtime.dbrep.config.resolution import runtime_root
from weaver_runtime.dbrep.runtime.orchestrator import load_target_runtime


def _setup(tmp_path: Path):
    ses_root = tmp_path / "SES"
    # Shared and database-level helper folders that must be carried into runtime.
    (ses_root / "_helpers").mkdir(parents=True)
    (ses_root / "_helpers" / "shared.py").write_text("SHARED = 1\n", encoding="utf-8")

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
    config = make_config(tmp_path, servers, databases)

    write_python_folder(ses_root / "T0", "Raw", "Drop")
    write_python_table(ses_root / "T1", "Stage", "Record", deps=("T0.Raw.Drop",))
    write_python_table(ses_root / "T1", "Mart", "RecordCurrent", deps=("Stage.Record",))
    (ses_root / "T1" / "_helpers").mkdir()
    (ses_root / "T1" / "_helpers" / "table_helpers.py").write_text("HELP = 1\n", encoding="utf-8")

    plan = plan_build(
        BuildRequest(
            pairs=(
                BuildPair(resolve(config, "T0_SES"), resolve(config, "T0_FILES")),
                BuildPair(resolve(config, "T1_SES"), resolve(config, "T1_DELTA")),
            )
        )
    )
    return config, plan


def test_install_writes_bundle_orchestrator_and_sources(tmp_path: Path) -> None:
    config, plan = _setup(tmp_path)
    result = install_build(plan, installed_at="2026-07-09T00:00:00Z")

    host = result.hosts[0]
    root = Path(host.runtime_root)
    assert root == runtime_root(resolve(config, "T1_DELTA"))

    # Orchestrator is bundled and self-contained.
    assert (root / "_orchestrator" / "weaver_load.py").is_file()
    assert (root / "_orchestrator" / "weaver_runtime" / "dbrep" / "objects.py").is_file()

    # Source snapshot preserves the discoverable layout, including _ helper folders.
    assert (root / "objects" / "T0" / "Raw__Drop.py").is_file()
    assert (root / "objects" / "T1" / "Stage__Record.py").is_file()
    assert (root / "objects" / "_helpers" / "shared.py").is_file()
    assert (root / "objects" / "T1" / "_helpers" / "table_helpers.py").is_file()


def test_install_writes_artifacts(tmp_path: Path) -> None:
    config, plan = _setup(tmp_path)
    result = install_build(plan, installed_at="2026-07-09T00:00:00Z")
    root = Path(result.hosts[0].runtime_root)

    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    catalogue = json.loads((root / "catalogue.json").read_text(encoding="utf-8"))
    load_dependency = json.loads((root / "load_dependency.json").read_text(encoding="utf-8"))
    table_dictionary = json.loads((root / "table_dictionary.json").read_text(encoding="utf-8"))
    column_dictionary = json.loads((root / "column_dictionary.json").read_text(encoding="utf-8"))
    index_dictionary = json.loads((root / "index_dictionary.json").read_text(encoding="utf-8"))
    foreign_key_dictionary = json.loads((root / "foreign_key_dictionary.json").read_text(encoding="utf-8"))

    assert manifest["object_count"] == 3
    assert "objects" not in manifest

    ids = {entry["id"] for entry in catalogue["objects"]}
    assert ids == {"T0.Raw.Drop", "T1.Stage.Record", "T1.Mart.RecordCurrent"}
    assert all(entry["source_hash"] for entry in catalogue["objects"])

    assert load_dependency["objects"]["T1.Stage.Record"] == ["T0.Raw.Drop"]
    assert load_dependency["objects"]["T1.Mart.RecordCurrent"] == ["T1.Stage.Record"]

    assert {entry["id"] for entry in table_dictionary["tables"]} == ids
    assert column_dictionary["columns"] == []
    assert {entry["object_id"] for entry in index_dictionary["indexes"]} == {
        "T1.Stage.Record",
        "T1.Mart.RecordCurrent",
    }
    assert foreign_key_dictionary["foreign_keys"] == []
    assert not (root / "load_plan.json").exists()
    assert not (root / "source_hashes.json").exists()


def test_install_creates_files_folder_and_marker(tmp_path: Path) -> None:
    config, plan = _setup(tmp_path)
    install_build(plan, installed_at="2026-07-09T00:00:00Z")

    lake = tmp_path / "lake"
    folder = lake / "Files" / "T0" / "Raw" / "Drop"
    assert folder.is_dir()
    marker = json.loads((folder / "_weaver.json").read_text(encoding="utf-8"))
    assert marker["id"] == "T0.Raw.Drop"
    assert marker["managed_by"] == "weaver"


def test_installed_runtime_validates_against_manifest(tmp_path: Path) -> None:
    config, plan = _setup(tmp_path)
    result = install_build(plan, installed_at="2026-07-09T00:00:00Z")
    root = Path(result.hosts[0].runtime_root)

    # Target-only validation (no execution, no Spark, no source repo access).
    report = load_target_runtime(root, execute=False)
    assert report.ok is True
    assert report.executed is False
    step_ids = [step.object_id for step in report.steps]
    assert step_ids[0] == "T0.Raw.Drop"
    assert "T1.Stage.Record" in step_ids


def test_installed_runtime_detects_source_hash_drift(tmp_path: Path) -> None:
    config, plan = _setup(tmp_path)
    result = install_build(plan, installed_at="2026-07-09T00:00:00Z")
    root = Path(result.hosts[0].runtime_root)

    # Tamper with an installed object file; hash validation must fail.
    tampered = root / "objects" / "T1" / "Stage__Record.py"
    tampered.write_text(tampered.read_text(encoding="utf-8") + "\n# drift\n", encoding="utf-8")

    from weaver_runtime.dbrep.errors import LoadError
    import pytest

    with pytest.raises(LoadError, match="source hash mismatch"):
        load_target_runtime(root, execute=False)


def test_sequential_build_merges_runtime_metadata(tmp_path: Path) -> None:
    config, _ = _setup(tmp_path)

    t0_plan = plan_build(
        BuildRequest(
            pairs=(BuildPair(resolve(config, "T0_SES"), resolve(config, "T0_FILES")),)
        )
    )
    install_build(t0_plan, installed_at="2026-07-09T00:00:00Z")

    t1_plan = plan_build(
        BuildRequest(
            pairs=(BuildPair(resolve(config, "T1_SES"), resolve(config, "T1_DELTA")),)
        )
    )
    result = install_build(t1_plan, installed_at="2026-07-09T00:01:00Z")
    root = Path(result.hosts[0].runtime_root)
    catalogue = json.loads((root / "catalogue.json").read_text(encoding="utf-8"))
    load_dependency = json.loads((root / "load_dependency.json").read_text(encoding="utf-8"))

    assert {entry["id"] for entry in catalogue["objects"]} == {
        "T0.Raw.Drop",
        "T1.Stage.Record",
        "T1.Mart.RecordCurrent",
    }
    assert (root / "objects" / "T0" / "Raw__Drop.py").is_file()
    assert load_dependency["objects"]["T0.Raw.Drop"] == []
    # T0 was external to the T1-only build, so it is preserved but not
    # introduced as an implicit T1 load dependency.
    assert load_dependency["objects"]["T1.Stage.Record"] == []
