from __future__ import annotations

from pathlib import Path

import pytest

from dbrep_helpers import (
    make_config,
    resolve,
    write_python_folder,
    write_python_table,
)
from weaver_runtime.dbrep.build import BuildPair, BuildRequest, format_dry_run, plan_build
from weaver_runtime.dbrep.errors import BuildError, GraphError


def _servers_and_databases(tmp_path: Path):
    ses_root = tmp_path / "SES"
    servers = {
        "SES_Repo": {"server": str(ses_root)},
        "Local_Lakehouse": {"server": str(tmp_path / "lake")},
    }
    databases = {
        "T0_SES": {"type": "SES", "server": "SES_Repo", "database": "T0"},
        "T1_SES": {"type": "SES", "server": "SES_Repo", "database": "T1"},
        "T0_FILES": {"type": "Files", "server": "Local_Lakehouse", "database": "T0"},
        "T1_DELTA": {"type": "Delta", "server": "Local_Lakehouse", "database": "T1"},
    }
    return ses_root, servers, databases


def _pair(config, source_alias, target_alias):
    return BuildPair(resolve(config, source_alias), resolve(config, target_alias))


def test_plan_orders_and_materialises(tmp_path: Path) -> None:
    ses_root, servers, databases = _servers_and_databases(tmp_path)
    config = make_config(tmp_path, servers, databases)
    write_python_folder(ses_root / "T0", "Raw", "Drop")
    write_python_table(ses_root / "T1", "Stage", "Record", deps=("T0.Raw.Drop",))
    write_python_table(ses_root / "T1", "Mart", "RecordCurrent", deps=("Stage.Record",))

    plan = plan_build(
        BuildRequest(
            pairs=(
                _pair(config, "T0_SES", "T0_FILES"),
                _pair(config, "T1_SES", "T1_DELTA"),
            )
        )
    )

    assert plan.order.index("T0.Raw.Drop") < plan.order.index("T1.Stage.Record")
    assert plan.order.index("T1.Stage.Record") < plan.order.index("T1.Mart.RecordCurrent")

    drop = plan.object_by_id("T0.Raw.Drop")
    stage = plan.object_by_id("T1.Stage.Record")
    mart = plan.object_by_id("T1.Mart.RecordCurrent")

    assert drop.materialisation == "Files/T0/Raw/Drop"
    assert stage.materialisation == "Tables/T1/Stage/Record"

    assert [(d.id, d.scope) for d in stage.dependencies] == [
        ("T0.Raw.Drop", "managed_cross_database")
    ]
    assert [(d.id, d.scope) for d in mart.dependencies] == [
        ("T1.Stage.Record", "intra_database")
    ]
    assert plan.external_dependencies == ()


def test_unsupplied_cross_database_dependency_is_external(tmp_path: Path) -> None:
    ses_root, servers, databases = _servers_and_databases(tmp_path)
    config = make_config(tmp_path, servers, databases)
    write_python_folder(ses_root / "T0", "Raw", "Drop")
    write_python_table(ses_root / "T1", "Stage", "Record", deps=("T0.Raw.Drop",))

    # Only T1 is supplied; T0.Raw.Drop must become external/stable.
    plan = plan_build(BuildRequest(pairs=(_pair(config, "T1_SES", "T1_DELTA"),)))

    stage = plan.object_by_id("T1.Stage.Record")
    assert stage.dependencies == ()
    assert [e.id for e in plan.external_dependencies] == ["T0.Raw.Drop"]


def test_missing_intra_database_dependency_fails(tmp_path: Path) -> None:
    ses_root, servers, databases = _servers_and_databases(tmp_path)
    config = make_config(tmp_path, servers, databases)
    write_python_table(ses_root / "T1", "Mart", "RecordCurrent", deps=("Stage.Missing",))

    with pytest.raises(BuildError, match="missing intra-database dependency"):
        plan_build(BuildRequest(pairs=(_pair(config, "T1_SES", "T1_DELTA"),)))


def test_missing_managed_cross_database_dependency_fails(tmp_path: Path) -> None:
    ses_root, servers, databases = _servers_and_databases(tmp_path)
    config = make_config(tmp_path, servers, databases)
    write_python_folder(ses_root / "T0", "Raw", "Drop")
    write_python_table(ses_root / "T1", "Stage", "Record", deps=("T0.Raw.Absent",))

    with pytest.raises(BuildError, match="missing managed cross-database dependency"):
        plan_build(
            BuildRequest(
                pairs=(
                    _pair(config, "T0_SES", "T0_FILES"),
                    _pair(config, "T1_SES", "T1_DELTA"),
                )
            )
        )


def test_cycle_fails(tmp_path: Path) -> None:
    ses_root, servers, databases = _servers_and_databases(tmp_path)
    config = make_config(tmp_path, servers, databases)
    write_python_table(ses_root / "T1", "Stage", "A", deps=("Stage.B",))
    write_python_table(ses_root / "T1", "Stage", "B", deps=("Stage.A",))

    with pytest.raises(GraphError, match="cycle detected"):
        plan_build(BuildRequest(pairs=(_pair(config, "T1_SES", "T1_DELTA"),)))


def test_empty_request_fails(tmp_path: Path) -> None:
    with pytest.raises(BuildError, match="at least one from/to pair"):
        plan_build(BuildRequest(pairs=()))


def test_dry_run_render(tmp_path: Path) -> None:
    ses_root, servers, databases = _servers_and_databases(tmp_path)
    config = make_config(tmp_path, servers, databases)
    write_python_folder(ses_root / "T0", "Raw", "Drop")

    plan = plan_build(BuildRequest(pairs=(_pair(config, "T0_SES", "T0_FILES"),), prune=True))
    rendered = format_dry_run(plan)
    assert "build plan (dry run)" in rendered
    assert "T0.Raw.Drop :: Folder -> Files/T0/Raw/Drop" in rendered
    assert "prune: enabled" in rendered
