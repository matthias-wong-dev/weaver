from __future__ import annotations

import json
from pathlib import Path

import pytest

from dbrep_helpers import write_config_files
from weaver_runtime.cli import main
from weaver_runtime.dbrep.build.manifest import read_json
from weaver_runtime.dbrep.runtime.load import _schema_by_object, _struct_type
from weaver_runtime.dbrep.runtime.orchestrator import load_target_runtime

pytestmark = pytest.mark.spark

FIXTURE_SES = Path(__file__).resolve().parents[1] / "fixtures" / "generic_ses" / "SES"

# Every declared Delta table across the T1/T2/T3 SES databases. Local Lakehouse
# hosts co-locate databases, so paths are Tables/<database>/<schema>/<object>.
DELTA_TABLES = {
    "T1.Stage.Record": "T1/Stage/Record",
    "T1.Mart.RecordAudit": "T1/Mart/RecordAudit",
    "T1.Mart.RecordSnapshot": "T1/Mart/RecordSnapshot",
    "T1.Mart.RecordCurrentAuto": "T1/Mart/RecordCurrentAuto",
    "T1.Mart.RecordCurrentKeep": "T1/Mart/RecordCurrentKeep",
    "T2.Mart.RecordAggregate": "T2/Mart/RecordAggregate",
    "T3.Report.RecordSummary": "T3/Report/RecordSummary",
}

# The superseded dotted layout must never be created.
OLD_DOTTED_RELATIVE = [
    "T1/Stage.Record",
    "T1/Mart.RecordAudit",
    "T1/Mart.RecordSnapshot",
    "T1/Mart.RecordCurrentAuto",
    "T1/Mart.RecordCurrentKeep",
    "T2/Mart.RecordAggregate",
    "T3/Report.RecordSummary",
]


def _expected_schema(runtime: Path, object_id: str):
    """Build the declared Spark schema for an object from the installed runtime."""

    column_dictionary = read_json(runtime / "column_dictionary.json")
    return _struct_type(_schema_by_object(column_dictionary)[object_id])


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

    # Build alone must materialise the full declared target structure, before any
    # input data exists or load runs: the T0 Folder object and every declared
    # Delta table as a valid zero-row table with the SES-declared schema.
    assert (lake / "Files" / "T0" / "Raw" / "Drop").is_dir()
    for object_id, relative in DELTA_TABLES.items():
        table_path = lake / "Tables" / relative
        assert (table_path / "_delta_log").is_dir(), f"{object_id} missing _delta_log"
        frame = spark.read.format("delta").load(str(table_path))
        assert frame.count() == 0, f"{object_id} was not created empty"
        assert frame.schema == _expected_schema(runtime, object_id)

    # A passing local build proves the generated Spark program actually ran: the
    # completion record is written only after execution and lists every table.
    record = json.loads((runtime / "build_complete.json").read_text(encoding="utf-8"))
    assert set(record["result"]["created"]) == set(DELTA_TABLES)

    # The superseded dotted layout must not exist.
    for old in OLD_DOTTED_RELATIVE:
        assert not (lake / "Tables" / old).exists(), f"old dotted path {old} exists"

    csv = tmp_path / "run1.csv"
    csv.write_text("record_id,group_id,amount\nr1,A,10\nr2,A,20\nr3,B,30\n", encoding="utf-8")
    monkeypatch.setenv("WEAVER_TEST_RUN_CSV", str(csv))

    report = load_target_runtime(runtime, execute=True, spark=spark)
    assert report.ok is True
    assert report.executed is True

    # One workflow directory is created under Files/_logs and reported back.
    assert report.workflow_id
    assert (lake / "Files" / "_logs" / report.workflow_id).is_dir()

    # Standard CRUD counts: the Folder is counted in files, the Table in rows.
    steps = {step.object_id: step for step in report.steps}
    drop = steps["T0.Raw.Drop"]
    assert drop.crud.unit == "files"
    assert (drop.crud.read, drop.crud.created, drop.crud.updated) == (1, 1, 0)
    stage_step = steps["T1.Stage.Record"]
    assert stage_step.crud.unit == "rows"
    assert (stage_step.crud.read, stage_step.crud.created) == (3, 3)

    # Folder object materialised under Files/, Delta tables under Tables/.
    assert (lake / "Files" / "T0" / "Raw" / "Drop" / "drop.csv").is_file()

    stage = spark.read.format("delta").load(str(lake / "Tables" / "T1" / "Stage" / "Record"))
    assert stage.count() == 3

    aggregate = {
        row["group_id"]: row["amount"]
        for row in spark.read.format("delta")
        .load(str(lake / "Tables" / "T2" / "Mart" / "RecordAggregate"))
        .collect()
    }
    assert aggregate == {"A": 30, "B": 30}

    summary = spark.read.format("delta").load(str(lake / "Tables" / "T3" / "Report" / "RecordSummary"))
    assert summary.count() == 2

    # A second load with changed drop.csv content: the Folder file is updated
    # (not recreated), proving file-level CRUD reconciliation across runs.
    csv2 = tmp_path / "run2.csv"
    csv2.write_text("record_id,group_id,amount\nr1,A,11\nr2,A,20\nr3,B,30\n", encoding="utf-8")
    monkeypatch.setenv("WEAVER_TEST_RUN_CSV", str(csv2))

    report2 = load_target_runtime(runtime, execute=True, spark=spark)
    assert report2.ok is True
    assert report2.workflow_id != report.workflow_id
    drop2 = {step.object_id: step for step in report2.steps}["T0.Raw.Drop"]
    assert (drop2.crud.read, drop2.crud.created, drop2.crud.updated) == (1, 0, 1)


def test_build_is_non_destructive(tmp_path: Path, spark, monkeypatch) -> None:
    """A rebuild must initialise missing tables but never touch existing data."""

    weaver_path = _write_config(tmp_path)
    _build(weaver_path)

    lake = tmp_path / "lake"
    runtime = lake / "Files" / "_weaver" / "runtime"

    csv = tmp_path / "run1.csv"
    csv.write_text("record_id,group_id,amount\nr1,A,10\nr2,A,20\nr3,B,30\n", encoding="utf-8")
    monkeypatch.setenv("WEAVER_TEST_RUN_CSV", str(csv))
    assert load_target_runtime(runtime, execute=True, spark=spark).ok is True

    def snapshot(relative: str):
        frame = spark.read.format("delta").load(str(lake / "Tables" / relative))
        rows = sorted(tuple(sorted(row.asDict().items())) for row in frame.collect())
        return frame.count(), frame.schema, rows

    before = {relative: snapshot(relative) for relative in DELTA_TABLES.values()}
    assert before["T1/Stage/Record"][0] == 3  # loaded data is present

    # Rebuild over the loaded lakehouse.
    _build(weaver_path)

    for relative, (count, schema, rows) in before.items():
        after = snapshot(relative)
        assert after == (count, schema, rows), f"{relative} changed on rebuild"


def test_wipe_then_build_recreates_empty_tables(tmp_path: Path, spark, monkeypatch) -> None:
    """Wiping a Delta target and rebuilding recreates its empty structure."""

    weaver_path = _write_config(tmp_path)
    _build(weaver_path)

    lake = tmp_path / "lake"
    runtime = lake / "Files" / "_weaver" / "runtime"

    csv = tmp_path / "run1.csv"
    csv.write_text("record_id,group_id,amount\nr1,A,10\nr2,A,20\nr3,B,30\n", encoding="utf-8")
    monkeypatch.setenv("WEAVER_TEST_RUN_CSV", str(csv))
    assert load_target_runtime(runtime, execute=True, spark=spark).ok is True
    assert spark.read.format("delta").load(str(lake / "Tables" / "T1" / "Stage" / "Record")).count() == 3

    # Wipe the T1 Delta target: its database directory must be gone.
    assert main(["wipe", "--config", str(weaver_path), "--target", "T1_LOCAL_DELTA"]) == 0
    assert not (lake / "Tables" / "T1").exists()

    # Rebuild recreates every declared T1 Delta table, empty, with its schema.
    _build(weaver_path)
    t1_tables = {
        object_id: relative
        for object_id, relative in DELTA_TABLES.items()
        if relative.startswith("T1/")
    }
    for object_id, relative in t1_tables.items():
        table_path = lake / "Tables" / relative
        assert (table_path / "_delta_log").is_dir(), f"{object_id} not recreated"
        frame = spark.read.format("delta").load(str(table_path))
        assert frame.count() == 0
        assert frame.schema == _expected_schema(runtime, object_id)


def test_build_fails_when_table_declares_no_schema(tmp_path: Path) -> None:
    """A Table object without a declared schema must fail the build clearly.

    Schema is validated while rendering the build program, before install or
    Spark, so this fails without materialising anything or running object code.
    """

    from dbrep_helpers import write_python_folder
    from weaver_runtime.dbrep.cli.commands import run_build
    from weaver_runtime.dbrep.errors import BuildError

    ses_root = tmp_path / "SES"
    servers = {
        "SES_Repo": {"server": str(ses_root)},
        "Local_Lakehouse": {"server": str(tmp_path / "lake")},
    }
    databases = {
        "T0_SES": {"type": "SES", "server": "SES_Repo", "database": "T0"},
        "T1_SES": {"type": "SES", "server": "SES_Repo", "database": "T1"},
        "T0_LOCAL_FILES": {"type": "Files", "server": "Local_Lakehouse", "database": "T0"},
        "T1_LOCAL_DELTA": {"type": "Delta", "server": "Local_Lakehouse", "database": "T1"},
    }
    weaver_path = write_config_files(tmp_path, servers, databases)
    write_python_folder(ses_root / "T0", "Raw", "Drop")

    # A Table with no Schema block whose read() would leave a sentinel if run.
    sentinel = tmp_path / "read_was_called.txt"
    (ses_root / "T1").mkdir(parents=True, exist_ok=True)
    (ses_root / "T1" / "Stage__NoSchema.py").write_text(
        '"""\n'
        "Table ID: Stage.NoSchema\n"
        "Description: A table without a declared schema.\n"
        "Lineage: Reads the raw drop directly.\n"
        "Primary key: record_id\n"
        '"""\n\n'
        "from weaver_runtime.dbrep.objects import Table\n\n\n"
        "class Stage__NoSchema(Table):\n"
        "    def read(self, spark):\n"
        f"        open({str(sentinel)!r}, 'w').close()\n"
        "        return None\n",
        encoding="utf-8",
    )

    with pytest.raises(BuildError) as excinfo:
        run_build(
            weaver_path,
            "T0_SES,T1_SES",
            "T0_LOCAL_FILES,T1_LOCAL_DELTA",
        )

    message = str(excinfo.value)
    assert "T1.Stage.NoSchema" in message  # identified by full ID
    assert "requires a declared schema" in message
    assert not sentinel.exists()  # read() was never executed
