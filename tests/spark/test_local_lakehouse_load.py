from __future__ import annotations

from pathlib import Path

import pytest

from dbrep_helpers import write_config_files
from weaver_runtime.cli import main
from weaver_runtime.dbrep.runtime.orchestrator import load_target_runtime

pytestmark = pytest.mark.spark

FIXTURE_SES = Path(__file__).resolve().parents[1] / "fixtures" / "generic_ses" / "SES"


def _write_config(tmp_path: Path) -> Path:
    servers = {
        "SES_Repo": {"server": str(FIXTURE_SES)},
        "Local_Lakehouse": {"server": str(tmp_path / "lake")},
    }
    databases = {
        "T0_SES": {"type": "SES", "server": "SES_Repo", "database": "T0"},
        "T1_SES": {"type": "SES", "server": "SES_Repo", "database": "T1"},
        "T2_SES": {"type": "SES", "server": "SES_Repo", "database": "T2"},
        "T3_SES": {"type": "SES", "server": "SES_Repo", "database": "T3"},
        "T0_LOCAL_FILES": {"type": "Files", "server": "Local_Lakehouse", "database": "T0"},
        "T1_LOCAL_DELTA": {"type": "Delta", "server": "Local_Lakehouse", "database": "T1"},
        "T2_LOCAL_DELTA": {"type": "Delta", "server": "Local_Lakehouse", "database": "T2"},
        "T3_LOCAL_DELTA": {"type": "Delta", "server": "Local_Lakehouse", "database": "T3"},
    }
    return write_config_files(tmp_path, servers, databases)


def _build(weaver_path: Path) -> None:
    code = main(
        [
            "build",
            "--config",
            str(weaver_path),
            "--from",
            "T0_SES,T1_SES,T2_SES,T3_SES",
            "--to",
            "T0_LOCAL_FILES,T1_LOCAL_DELTA,T2_LOCAL_DELTA,T3_LOCAL_DELTA",
        ]
    )
    assert code == 0


def test_local_lakehouse_build_and_load(tmp_path: Path, spark, monkeypatch) -> None:
    weaver_path = _write_config(tmp_path)
    _build(weaver_path)

    lake = tmp_path / "lake"
    runtime = lake / "Files" / "_weaver" / "runtime"

    csv = tmp_path / "run1.csv"
    csv.write_text("record_id,group_id,amount\nr1,A,10\nr2,A,20\nr3,B,30\n", encoding="utf-8")
    monkeypatch.setenv("WEAVER_TEST_RUN_CSV", str(csv))

    report = load_target_runtime(runtime, execute=True, spark=spark)
    assert report.ok is True
    assert report.executed is True

    # Folder object materialised under Files/, Delta tables under Tables/.
    assert (lake / "Files" / "T0" / "Raw" / "Drop" / "drop.csv").is_file()

    stage = spark.read.format("delta").load(str(lake / "Tables" / "T1" / "Stage.Record"))
    assert stage.count() == 3

    aggregate = {
        row["group_id"]: row["amount"]
        for row in spark.read.format("delta")
        .load(str(lake / "Tables" / "T2" / "Mart.RecordAggregate"))
        .collect()
    }
    assert aggregate == {"A": 30, "B": 30}

    summary = spark.read.format("delta").load(str(lake / "Tables" / "T3" / "Report.RecordSummary"))
    assert summary.count() == 2
