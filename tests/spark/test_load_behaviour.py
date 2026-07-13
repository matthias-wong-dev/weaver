from __future__ import annotations

from pathlib import Path

import pytest

from dbrep_helpers import write_config_files
from weaver_runtime.cli import main
from weaver_runtime.dbrep.runtime.orchestrator import load_target_runtime

pytestmark = pytest.mark.spark

FIXTURE_SES = Path(__file__).resolve().parents[1] / "fixtures" / "generic_ses" / "SES"

RUN_1 = "record_id,group_id,amount\nr1,A,10\nr2,A,20\nr3,B,30\n"
RUN_2 = "record_id,group_id,amount\nr1,A,10\nr2,A,22\nr3,B,31\nr3,B,32\n,A,5\n"
RUN_3 = "record_id,group_id,amount\nr2,A,22\nr3,B,33\nr4,B,40\n"


def _write_config(tmp_path: Path) -> Path:
    servers = {
        "SES_Repo": {"server": str(FIXTURE_SES)},
        "Local_Lakehouse": {"server": str(tmp_path / "lake")},
    }
    databases = {
        "T0_SES": {"type": "SES", "server": "SES_Repo", "database": "T0"},
        "T1_SES": {"type": "SES", "server": "SES_Repo", "database": "T1"},
        "T2_SES": {"type": "SES", "server": "SES_Repo", "database": "T2"},
        "T0_LOCAL_FILES": {"type": "Files", "server": "Local_Lakehouse", "database": "T0"},
        "T1_LOCAL_DELTA": {"type": "Delta", "server": "Local_Lakehouse", "database": "T1"},
        "T2_LOCAL_DELTA": {"type": "Delta", "server": "Local_Lakehouse", "database": "T2"},
    }
    return write_config_files(tmp_path, servers, databases)


def _table(spark, path: Path) -> dict:
    return {
        row["record_id"]: row["amount"]
        for row in spark.read.format("delta").load(str(path)).collect()
    }


def test_three_run_load_behaviour(tmp_path: Path, spark, monkeypatch) -> None:
    weaver_path = _write_config(tmp_path)
    assert (
        main(
            [
                "build",
                "--config",
                str(weaver_path),
                "--from",
                "T0_SES,T1_SES,T2_SES",
                "--to",
                "T0_LOCAL_FILES,T1_LOCAL_DELTA,T2_LOCAL_DELTA",
            ]
        )
        == 0
    )

    lake = tmp_path / "lake"
    runtime = lake / "Files" / "_weaver" / "runtime"
    auto = lake / "Tables" / "T1" / "Mart" / "RecordCurrentAuto"
    keep = lake / "Tables" / "T1" / "Mart" / "RecordCurrentKeep"
    audit = lake / "Tables" / "T1" / "Mart" / "RecordAudit"
    snapshot = lake / "Tables" / "T1" / "Mart" / "RecordSnapshot"
    aggregate = lake / "Tables" / "T2" / "Mart" / "RecordAggregate"
    rejects = lake / "Files" / "_weaver" / "logs" / "rejects"

    def run(content: str):
        csv = tmp_path / "run.csv"
        csv.write_text(content, encoding="utf-8")
        monkeypatch.setenv("WEAVER_TEST_RUN_CSV", str(csv))
        report = load_target_runtime(runtime, execute=True, spark=spark)
        assert report.ok is True
        return report

    # Run 1: clean load.
    run(RUN_1)
    assert _table(spark, auto) == {"r1": 10, "r2": 20, "r3": 30}
    assert _table(spark, keep) == {"r1": 10, "r2": 20, "r3": 30}
    assert spark.read.format("delta").load(str(audit)).count() == 3
    assert spark.read.format("delta").load(str(snapshot)).count() == 3

    # Run 2: duplicate r3 + blank record_id -> rejects; auto-delete suppressed.
    run(RUN_2)
    assert _table(spark, auto) == {"r1": 10, "r2": 22, "r3": 30}  # r1 NOT deleted
    assert _table(spark, keep) == {"r1": 10, "r2": 22, "r3": 30}
    assert (rejects / "T1.Mart.RecordCurrentAuto" / "rejects.json").is_file()
    # The audit's artificial UUID primary key plus Auto delete: false inserts
    # every batch: 3 (run 1) + 5 (run 2) = 8 distinct audit records.
    audit_frame = spark.read.format("delta").load(str(audit))
    assert audit_frame.count() == 8
    assert audit_frame.select("audit_id").distinct().count() == 8
    # The no-key default is replacement: only run 2's five rows remain.
    assert spark.read.format("delta").load(str(snapshot)).count() == 5

    # Run 3: clean batch -> auto-delete removes the now-missing r1.
    run3 = run(RUN_3)
    assert _table(spark, auto) == {"r2": 22, "r3": 33, "r4": 40}  # r1 deleted
    assert _table(spark, keep) == {"r1": 10, "r2": 22, "r3": 33, "r4": 40}  # r1 retained
    assert spark.read.format("delta").load(str(audit)).count() == 11
    assert _table(spark, snapshot) == {"r2": 22, "r3": 33, "r4": 40}

    # Stage.Record does not declare Auto delete. Its primary key makes the table
    # default to auto-delete=true, so run 3 removes the missing r1 before the
    # downstream aggregate is calculated.
    stage_step = next(step for step in run3.steps if step.object_id == "T1.Stage.Record")
    assert stage_step.details["auto_delete_ran"] is True
    assert stage_step.crud.deleted == 1
    assert {
        row["group_id"]: row["amount"]
        for row in spark.read.format("delta").load(str(aggregate)).collect()
    } == {"A": 22, "B": 73}
