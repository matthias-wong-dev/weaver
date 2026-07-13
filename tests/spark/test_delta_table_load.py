from __future__ import annotations

from pathlib import Path

import pytest

from weaver_runtime.dbrep.runtime.delta_table_load import execute_delta_table_load
from weaver_runtime.dbrep.runtime.load_policy import run_table_load
from weaver_runtime.dbrep.runtime.spark_io import struct_type

pytestmark = pytest.mark.spark

SCHEMA = (("record_id", "string"), ("amount", "int"))


def _frame(spark, rows):
    return spark.createDataFrame(rows, schema=struct_type(SCHEMA))


def _write(spark, path: Path, rows) -> None:
    _frame(spark, rows).write.format("delta").mode("overwrite").save(str(path))


def _rows(spark, path: Path) -> list[dict]:
    return [
        row.asDict(recursive=True)
        for row in spark.read.format("delta").load(str(path)).collect()
    ]


def _version(spark, path: Path) -> int:
    from delta.tables import DeltaTable

    return int(DeltaTable.forPath(spark, str(path)).history(1).head()["version"])


def test_empty_incremental_load_is_a_true_delta_noop(tmp_path: Path, spark) -> None:
    path = tmp_path / "empty_incremental"
    existing = [("r1", 10), ("r2", 20)]
    _write(spark, path, existing)
    before_version = _version(spark, path)

    outcome = execute_delta_table_load(
        spark,
        _frame(spark, []),
        path,
        primary_key=("record_id",),
        schema=SCHEMA,
        is_incremental=True,
        object_name="T1.Mart.Record",
    )

    assert outcome.counts() == {
        "input": 0,
        "accepted": 0,
        "rejected": 0,
        "inserted": 0,
        "updated": 0,
        "deleted": 0,
    }
    assert outcome.wrote is False
    assert _version(spark, path) == before_version
    assert sorted(_rows(spark, path), key=lambda row: row["record_id"]) == [
        {"record_id": "r1", "amount": 10},
        {"record_id": "r2", "amount": 20},
    ]


@pytest.mark.parametrize(
    ("name", "incoming", "is_incremental", "delete_keys"),
    [
        ("incremental", [("r2", 22), ("r4", 40)], True, ()),
        ("complete", [("r2", 22), ("r4", 40)], False, ()),
        ("explicit", [("r2", 22), ("r4", 40)], True, (("r1",), ("r2",))),
        ("rejects", [("r2", 22), ("r2", 22), (None, 5)], True, ()),
    ],
)
def test_native_delta_execution_matches_reference_policy(
    tmp_path: Path,
    spark,
    name,
    incoming,
    is_incremental,
    delete_keys,
) -> None:
    path = tmp_path / name
    existing = [
        {"record_id": "r1", "amount": 10},
        {"record_id": "r2", "amount": 20},
        {"record_id": "r3", "amount": 30},
    ]
    _write(
        spark,
        path,
        [(row["record_id"], row["amount"]) for row in existing],
    )
    incoming_rows = [
        {"record_id": record_id, "amount": amount}
        for record_id, amount in incoming
    ]
    reference = run_table_load(
        existing,
        incoming_rows,
        primary_key=("record_id",),
        schema=SCHEMA,
        is_incremental=is_incremental,
        explicit_delete_keys=delete_keys,
        object_name="T1.Mart.Record",
    )

    outcome = execute_delta_table_load(
        spark,
        _frame(spark, incoming),
        path,
        primary_key=("record_id",),
        schema=SCHEMA,
        is_incremental=is_incremental,
        explicit_delete_keys=delete_keys,
        object_name="T1.Mart.Record",
    )

    native_rows = sorted(
        _rows(spark, path), key=lambda row: (row["record_id"] or "")
    )
    reference_rows = sorted(
        reference.final_rows, key=lambda row: (row["record_id"] or "")
    )
    assert native_rows == reference_rows
    assert outcome.counts() == reference.counts()
    assert {
        row["_reject_reason"] for row in outcome.rejected
    } == {row["_reject_reason"] for row in reference.rejected}
    assert outcome.explicit_delete_keys_read == reference.explicit_delete_keys_read
    assert outcome.explicit_delete_keys_matched == reference.explicit_delete_keys_matched
    assert outcome.explicit_delete_keys_unmatched == reference.explicit_delete_keys_unmatched
