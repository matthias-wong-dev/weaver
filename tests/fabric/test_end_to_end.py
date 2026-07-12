from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from dbrep_helpers import write_config_files
from fabric_helpers import view_rows
from weaver_runtime.dbrep.cli.commands import run_build, run_load

pytestmark = pytest.mark.fabric


def _write_fixture(root: Path) -> None:
    (root / "T0").mkdir(parents=True, exist_ok=True)
    (root / "T1").mkdir(parents=True, exist_ok=True)
    (root / "T2").mkdir(parents=True, exist_ok=True)

    # T0 -> Files (Lakehouse): a Folder that writes a fixed seed CSV.
    (root / "T0" / "Raw__Drop.py").write_text(
        textwrap.dedent(
            '''\
            """
            Folder ID: Raw.Drop
            Description: Raw record drop folder.
            Lineage: Writes a fixed seed CSV into the landing folder.
            File key: "**/*.csv"
            Auto delete: false
            """
            from weaver_runtime.dbrep.objects import Folder

            class Raw__Drop(Folder):
                def read(self):
                    with self.staging_folder() as staging:
                        (staging.path / "drop.csv").write_text(
                            "record_id,group_id,amount\\nr1,A,10\\nr2,A,20\\nr3,B,30\\n",
                            encoding="utf-8",
                        )
                    return staging, ()
            '''
        ),
        encoding="utf-8",
    )

    # T1 -> Delta (Lakehouse): typed stage table + aggregate.
    (root / "T1" / "Stage__Record.py").write_text(
        textwrap.dedent(
            '''\
            """
            Table ID: Stage.Record
            Description: Normalised records.
            Lineage: Reads the raw drop CSV and types it.
            Primary key: record_id
            Schema:
              record_id: string
              group_id: string
              amount: int
            """
            from weaver_runtime.dbrep.objects import Table

            class Stage__Record(Table):
                def read(self, spark):
                    drop = self.repo["T0.Raw.Drop"]
                    return spark.read.option("header", True).csv(f"{drop}/drop.csv"), ()
            '''
        ),
        encoding="utf-8",
    )
    (root / "T1" / "Mart__Aggregate.py").write_text(
        textwrap.dedent(
            '''\
            """
            Table ID: Mart.Aggregate
            Description: Amount per group.
            Lineage: Aggregates the typed stage records by group.
            Primary key: group_id
            Schema:
              group_id: string
              amount: long
            """
            from weaver_runtime.dbrep.objects import Table

            class Mart__Aggregate(Table):
                def read(self, spark):
                    from pyspark.sql import functions as F
                    stage = self.repo["T1.Stage.Record"]
                    return stage.groupBy("group_id").agg(F.sum("amount").alias("amount")), ()
            '''
        ),
        encoding="utf-8",
    )

    # T2 -> SQL (Warehouse): self-contained seed -> aggregate -> view.
    (root / "T2" / "raw.Seed.sql").write_text(
        textwrap.dedent(
            """\
            /*
            Table ID: raw.Seed
            Description: Seed rows.
            Lineage: Emits a small fixed set of seed rows.
            Primary key: record_id
            */
            select record_id, group_id, amount
            from (values ('r1','A',10), ('r2','A',20), ('r3','B',30))
                 as v(record_id, group_id, amount)
            """
        ),
        encoding="utf-8",
    )
    (root / "T2" / "mart.Aggregate.sql").write_text(
        textwrap.dedent(
            """\
            /*
            Table ID: mart.Aggregate
            Description: Amount per group.
            Lineage: Aggregates the seed rows by group.
            Primary key: group_id
            */
            select group_id, sum(amount) as amount from raw.Seed group by group_id
            """
        ),
        encoding="utf-8",
    )
    (root / "T2" / "report.Summary.sql").write_text(
        textwrap.dedent(
            """\
            /*
            View ID: report.Summary
            Description: Summary view.
            Lineage: Reads the aggregate.
            */
            select group_id, amount from mart.Aggregate
            """
        ),
        encoding="utf-8",
    )


def _config(tmp_path: Path, sql_target, lakehouse_target) -> Path:
    servers = {
        "SES_Repo": {"type": "SES", "server": str(tmp_path / "SES")},
        "Fabric_LH": {
            "type": "Fabric Lakehouse",
            "server": f"{lakehouse_target['workspace']}/{lakehouse_target['lakehouse']}",
        },
        "Warehouse": {"type": "SQL", "server": sql_target["server"], "degrees_of_parallelism": sql_target["dop"]},
    }
    databases = {
        "T0_SES": {"type": "SES", "server": "SES_Repo", "database": "T0"},
        "T1_SES": {"type": "SES", "server": "SES_Repo", "database": "T1"},
        "T2_SES": {"type": "SES", "server": "SES_Repo", "database": "T2"},
        "T0_FILES": {"type": "Files", "server": "Fabric_LH", "database": "T0"},
        "T1_DELTA": {"type": "Delta", "server": "Fabric_LH", "database": "T1"},
        "T2_SQL": {"type": "SQL", "server": "Warehouse", "database": sql_target["database"]},
    }
    return write_config_files(tmp_path, servers, databases)


def test_end_to_end_lakehouse_and_sql(tmp_path: Path, clean_fabric_sql, fabric_lakehouse_target) -> None:
    sql_target = clean_fabric_sql
    _write_fixture(tmp_path / "SES")
    weaver = _config(tmp_path, sql_target, fabric_lakehouse_target)

    # Build all three representations in one command.
    built = run_build(
        weaver,
        "T0_SES,T1_SES,T2_SES",
        "T0_FILES,T1_DELTA,T2_SQL",
        prune=True,
    )
    assert built["built"] == [
        "T0.Raw.Drop",
        "T1.Stage.Record",
        "T1.Mart.Aggregate",
        "T2.raw.Seed",
        "T2.mart.Aggregate",
        "T2.report.Summary",
    ]
    assert built["fabric"][0]["lakehouse"] == fabric_lakehouse_target["lakehouse"]

    # Loads are target-scoped (a load of one alias must not run every co-located
    # target). Load the Files target first so the T0 Folder writes its seed CSV,
    # then the Delta target which reads it. Assert on accepted rows (stable
    # across reruns; inserts become upserts on a rerun).
    files_load = run_load(weaver, "T0_FILES")
    files_report = files_load["report"]
    files_steps = {step["object_id"]: step for step in files_report["steps"]}
    assert files_report["ok"] is True
    # The same generated runtime that runs locally mints a workflow id and a
    # durable Files/_logs/<workflow_id> directory, and returns both.
    assert files_report["workflow_id"]
    assert files_report["log_dir"].endswith(files_report["workflow_id"])
    assert "Files/_logs" in files_report["log_dir"]
    drop = files_steps["T0.Raw.Drop"]
    assert drop["status"] == "success"
    assert drop["kind"] == "Folder"
    assert drop["module"] == "Raw__Drop.py"  # object/module names live in the JSON
    assert drop["crud"]["unit"] == "files"  # Folder CRUD counts files

    lakehouse_load = run_load(weaver, "T1_DELTA")
    report = lakehouse_load["report"]
    assert report["ok"] is True
    assert report["workflow_id"] and report["workflow_id"] != files_report["workflow_id"]
    counts = {step["object_id"]: step for step in report["steps"]}
    # Table CRUD is counted in rows; accepted rows (stable across reruns) live in
    # the supplementary details block.
    assert counts["T1.Stage.Record"]["crud"]["unit"] == "rows"
    assert counts["T1.Stage.Record"]["details"]["accepted"] == 3
    assert counts["T1.Mart.Aggregate"]["details"]["accepted"] == 2

    # Load the SQL warehouse (T2) via installed stored procedures.
    sql_load = run_load(weaver, "T2_SQL")
    assert sql_load["executed"] is True

    # The final T2 view sees the loaded data.
    summary = {
        row["group_id"]: row["amount"]
        for row in view_rows(sql_target["server"], sql_target["database"], "report.Summary")
    }
    assert summary == {"A": 30, "B": 30}
