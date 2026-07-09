from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from dbrep_helpers import make_config, resolve, write_config_files, write_python_table
from weaver_runtime.dbrep.build import BuildPair, BuildRequest, plan_build
from weaver_runtime.dbrep.build.runtime_bundle import install_build
from weaver_runtime.dbrep.cli.commands import run_load
from weaver_runtime.dbrep.errors import BuildError
from weaver_runtime.dbrep.sql.backend import build_sql_target
from weaver_runtime.dbrep.targets.fabric_lakehouse import FabricLakehouseHost


def _sql_setup(tmp_path: Path):
    ses_root = tmp_path / "SES"
    servers = {
        "SES_Repo": {"server": str(ses_root)},
        "Warehouse": {"server": "endpoint.example.com", "degrees_of_parallelism": 8},
    }
    databases = {
        "T2_SES": {"type": "SES", "server": "SES_Repo", "database": "T2"},
        "T2_SQL": {"type": "SQL", "server": "Warehouse", "database": "T2"},
    }
    config = make_config(tmp_path, servers, databases)
    write_python_table(ses_root / "T2", "Mart", "Aggregate", primary_key="group_id")
    # A View object declared as SQL.
    (ses_root / "T2" / "Report.Summary.sql").write_text(
        textwrap.dedent(
            """
            /*
            View ID: Report.Summary
            Description: Summary view.
            Lineage: Reads the aggregate.
            */
            select group_id, amount from Mart.Aggregate
            """
        ),
        encoding="utf-8",
    )
    return config


def test_sql_build_is_plan_only_with_dop_and_procedure(tmp_path: Path) -> None:
    config = _sql_setup(tmp_path)
    plan = plan_build(
        BuildRequest(pairs=(BuildPair(resolve(config, "T2_SES"), resolve(config, "T2_SQL")),))
    )
    result = install_build(plan)

    # No Lakehouse host runtime bundle is written for a SQL-only build.
    assert result.hosts == ()
    assert len(result.sql) == 1
    sql = result.sql[0]
    assert sql.target == "T2_SQL"
    assert sql.degrees_of_parallelism == 8
    assert sql.load_procedure == "_weaver.load"

    operations = {op for action in sql.actions for op in action.operations}
    assert "create schema" in operations
    assert "install load stored procedure" in operations
    assert "create or replace view" in operations  # from the View object


def test_sql_load_dry_run_describes_procedure(tmp_path: Path) -> None:
    _sql_setup(tmp_path)
    weaver = write_config_files(
        tmp_path,
        {
            "SES_Repo": {"server": str(tmp_path / "SES")},
            "Warehouse": {"server": "endpoint.example.com", "degrees_of_parallelism": 8},
        },
        {
            "T2_SES": {"type": "SES", "server": "SES_Repo", "database": "T2"},
            "T2_SQL": {"type": "SQL", "server": "Warehouse", "database": "T2"},
        },
    )
    descriptor = run_load(weaver, "T2_SQL", dry_run=True)
    assert descriptor["type"] == "SQL"
    assert descriptor["degrees_of_parallelism"] == 8
    assert descriptor["load_procedure"] == "_weaver.load"
    assert descriptor["executed"] is False


def test_sql_build_requires_sql_source_objects(tmp_path: Path) -> None:
    # A Python-authored object cannot install to a SQL target: the SQL backend
    # validates source language before touching the network.
    config = _sql_setup(tmp_path)
    plan = plan_build(
        BuildRequest(pairs=(BuildPair(resolve(config, "T2_SES"), resolve(config, "T2_SQL")),))
    )
    target = resolve(config, "T2_SQL")
    python_objects = [o for o in plan.objects if o.source.language == "python"]
    assert python_objects  # _sql_setup writes a python Mart.Aggregate
    with pytest.raises(BuildError, match="requires .sql source objects"):
        build_sql_target(python_objects, target)


def test_fabric_host_interface_is_documented_stub(tmp_path: Path) -> None:
    config = _sql_setup(tmp_path)
    # A Fabric host can be constructed from a resolved target without changing the
    # model, but installing is intentionally deferred.
    host = FabricLakehouseHost(host="Workspace/Warehouse")
    with pytest.raises(BuildError, match="not implemented"):
        host.install()
