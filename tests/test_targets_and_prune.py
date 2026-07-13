from __future__ import annotations

import json
from pathlib import Path

import pytest

from dbrep_helpers import (
    make_config,
    resolve,
    write_python_folder,
    write_python_table,
)
from weaver_runtime.dbrep.build import BuildPair, BuildRequest, plan_build
from weaver_runtime.dbrep.build.compatibility import validate_object_kind, validate_pair
from weaver_runtime.dbrep.build.prune import PreviousObject, plan_prune
from weaver_runtime.dbrep.config.resolution import lakehouse_root
from weaver_runtime.dbrep.errors import BuildError, CompatibilityError
from weaver_runtime.dbrep.targets import get_adapter


def _config(tmp_path: Path):
    ses_root = tmp_path / "SES"
    servers = {
        "SES_Repo": {"server": str(ses_root)},
        "Lake": {"server": str(tmp_path / "lake")},
        "Warehouse": {"server": "endpoint.example.com", "degrees_of_parallelism": 8},
    }
    databases = {
        "T0_SES": {"type": "SES", "server": "SES_Repo", "database": "T0"},
        "T1_SES": {"type": "SES", "server": "SES_Repo", "database": "T1"},
        "T2_SES": {"type": "SES", "server": "SES_Repo", "database": "T2"},
        "T0_FILES": {"type": "Files", "server": "Lake", "database": "T0"},
        "T1_DELTA": {"type": "Delta", "server": "Lake", "database": "T1"},
        "T2_SQL": {"type": "SQL", "server": "Warehouse", "database": "T2"},
    }
    return ses_root, make_config(tmp_path, servers, databases)


# --- compatibility ---------------------------------------------------------


def test_validate_pair_requires_ses_source() -> None:
    validate_pair("SES", "Files")
    with pytest.raises(CompatibilityError, match="sources must be SES"):
        validate_pair("Files", "Delta")


def test_kind_type_compatibility() -> None:
    validate_object_kind("Folder", "Files")
    validate_object_kind("Table", "Delta")
    validate_object_kind("Table", "SQL")
    validate_object_kind("View", "SQL")
    with pytest.raises(CompatibilityError, match="Table objects cannot install to a Files"):
        validate_object_kind("Table", "Files")
    with pytest.raises(CompatibilityError, match="Folder objects cannot install to a Delta"):
        validate_object_kind("Folder", "Delta")
    with pytest.raises(CompatibilityError, match="cannot install to a SQL"):
        validate_object_kind("Folder", "SQL")


def test_plan_build_rejects_incompatible_kind(tmp_path: Path) -> None:
    ses_root, config = _config(tmp_path)
    # A Table declared but pointed at a Files target.
    write_python_table(ses_root / "T0", "Stage", "Record")
    with pytest.raises(CompatibilityError):
        plan_build(
            BuildRequest(pairs=(BuildPair(resolve(config, "T0_SES"), resolve(config, "T0_FILES")),))
        )


# --- adapters --------------------------------------------------------------


def test_files_adapter_creates_folder_and_marker(tmp_path: Path) -> None:
    ses_root, config = _config(tmp_path)
    write_python_folder(ses_root / "T0", "Raw", "Drop")
    plan = plan_build(
        BuildRequest(pairs=(BuildPair(resolve(config, "T0_SES"), resolve(config, "T0_FILES")),))
    )
    planned = plan.object_by_id("T0.Raw.Drop")
    target = resolve(config, "T0_FILES")
    host_root = lakehouse_root(target)

    adapter = get_adapter("Files")
    action = adapter.apply(planned, host_root)

    assert action.applied is True
    folder = host_root / "Files/T0/Raw/Drop"
    assert folder.is_dir()
    marker = json.loads((folder / "_weaver.json").read_text(encoding="utf-8"))
    assert marker["managed_by"] == "weaver"
    assert marker["id"] == "T0.Raw.Drop"


def test_delta_adapter_is_plan_only_with_policy_operations(tmp_path: Path) -> None:
    ses_root, config = _config(tmp_path)
    write_python_table(
        ses_root / "T1", "Stage", "Record",
        primary_key="record_id", is_incremental=False,
        schema_cols=(("record_id", "string"), ("amount", "int")),
    )
    plan = plan_build(
        BuildRequest(pairs=(BuildPair(resolve(config, "T1_SES"), resolve(config, "T1_DELTA")),))
    )
    planned = plan.object_by_id("T1.Stage.Record")
    action = get_adapter("Delta").apply(planned, lakehouse_root(resolve(config, "T1_DELTA")))

    assert action.applied is False  # plan-only
    assert "apply schema declaration" in action.operations
    assert "record primary key" in action.operations
    assert "record complete-reconciliation policy" in action.operations


def test_sql_adapter_plans_view_and_table(tmp_path: Path) -> None:
    ses_root, config = _config(tmp_path)
    write_python_table(ses_root / "T2", "Mart", "Aggregate", primary_key="group_id")
    plan = plan_build(
        BuildRequest(pairs=(BuildPair(resolve(config, "T2_SES"), resolve(config, "T2_SQL")),))
    )
    planned = plan.object_by_id("T2.Mart.Aggregate")
    action = get_adapter("SQL").plan(planned, None)
    assert action.applied is False
    assert "install load stored procedure" in action.operations
    assert action.materialisation == "T2.Mart.Aggregate"


def test_unknown_target_type_has_no_adapter() -> None:
    with pytest.raises(BuildError, match="no target adapter"):
        get_adapter("Parquet")


# --- prune -----------------------------------------------------------------


def test_prune_only_affects_previously_managed_within_covered_targets(tmp_path: Path) -> None:
    ses_root, config = _config(tmp_path)
    write_python_folder(ses_root / "T0", "Raw", "Drop")
    plan = plan_build(
        BuildRequest(
            pairs=(BuildPair(resolve(config, "T0_SES"), resolve(config, "T0_FILES")),),
            prune=True,
        )
    )

    previous = [
        # Still declared -> keep.
        PreviousObject("T0.Raw.Drop", "Folder", "Files/T0/Raw/Drop", "T0_FILES"),
        # No longer declared, same target -> prune.
        PreviousObject("T0.Raw.Old", "Folder", "Files/T0/Raw/Old", "T0_FILES"),
        # No longer declared but different target not covered -> keep.
        PreviousObject("T1.Stage.Gone", "Table", "Tables/T1/Stage.Gone", "T1_DELTA"),
    ]

    removed = plan_prune(plan, previous)
    assert [item.id for item in removed] == ["T0.Raw.Old"]
