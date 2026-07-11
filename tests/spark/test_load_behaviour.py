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

    # Run 2: duplicate r3 + blank record_id -> rejects; auto-delete suppressed.
    run(RUN_2)
    assert _table(spark, auto) == {"r1": 10, "r2": 22, "r3": 30}  # r1 NOT deleted
    assert _table(spark, keep) == {"r1": 10, "r2": 22, "r3": 30}
    assert (rejects / "T1.Mart.RecordCurrentAuto" / "rejects.json").is_file()
    # Audit is append-only: 3 (run1) + 5 (run2 raw rows) = 8.
    assert spark.read.format("delta").load(str(audit)).count() == 8

    # Run 3: clean batch -> auto-delete removes the now-missing r1.
    run(RUN_3)
    assert _table(spark, auto) == {"r2": 22, "r3": 33, "r4": 40}  # r1 deleted
    assert _table(spark, keep) == {"r1": 10, "r2": 22, "r3": 33, "r4": 40}  # r1 retained

    # Aggregate reflects the cleaned stage table (keep-missing): A=10+22, B=33+40.
    assert {
        row["group_id"]: row["amount"]
        for row in spark.read.format("delta").load(str(aggregate)).collect()
    } == {"A": 32, "B": 73}
