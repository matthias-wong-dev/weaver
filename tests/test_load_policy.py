from __future__ import annotations

import pytest

from weaver_runtime.dbrep.errors import LoadError
from weaver_runtime.dbrep.runtime.load_policy import (
    REASON_BLANK_PK,
    REASON_CAST,
    REASON_DUPLICATE_PK,
    run_table_load,
)

SCHEMA = (("record_id", "string"), ("group_id", "string"), ("amount", "int"))


def _rows(*triples):
    return [
        {"record_id": r, "group_id": g, "amount": a} for r, g, a in triples
    ]


def _by_id(outcome):
    return {row["record_id"]: row["amount"] for row in outcome.final_rows}


def _reasons(outcome):
    return sorted(row["_reject_reason"] for row in outcome.rejected)


def test_no_primary_key_is_append_only() -> None:
    existing = _rows(("r1", "A", 10))
    incoming = _rows(("r1", "A", 99), ("r2", "A", 20))
    outcome = run_table_load(existing, incoming, primary_key=(), schema=SCHEMA)
    # Append keeps everything, including the duplicate business key.
    assert len(outcome.final_rows) == 3
    assert outcome.inserted == 2
    assert outcome.deleted == 0


def test_primary_key_keep_missing_upsert() -> None:
    existing = _rows(("r1", "A", 10), ("r2", "A", 20), ("r3", "B", 30))
    incoming = _rows(("r2", "A", 22), ("r3", "B", 33), ("r4", "B", 40))
    outcome = run_table_load(
        existing, incoming, primary_key=("record_id",), schema=SCHEMA, auto_delete=False
    )
    result = _by_id(outcome)
    assert result == {"r1": 10, "r2": 22, "r3": 33, "r4": 40}  # r1 kept
    assert outcome.inserted == 1
    assert outcome.updated == 2
    assert outcome.deleted == 0
    assert outcome.auto_delete_ran is False


def test_primary_key_auto_delete_removes_missing() -> None:
    existing = _rows(("r1", "A", 10), ("r2", "A", 20), ("r3", "B", 30))
    incoming = _rows(("r2", "A", 22), ("r3", "B", 33), ("r4", "B", 40))
    outcome = run_table_load(
        existing, incoming, primary_key=("record_id",), schema=SCHEMA, auto_delete=True
    )
    result = _by_id(outcome)
    assert result == {"r2": 22, "r3": 33, "r4": 40}  # r1 deleted
    assert outcome.deleted == 1
    assert outcome.auto_delete_ran is True


def test_blank_primary_key_is_rejected() -> None:
    incoming = _rows(("r1", "A", 10)) + [{"record_id": "", "group_id": "A", "amount": 5}]
    outcome = run_table_load([], incoming, primary_key=("record_id",), schema=SCHEMA)
    assert REASON_BLANK_PK in _reasons(outcome)
    assert _by_id(outcome) == {"r1": 10}


def test_duplicate_incoming_primary_key_is_rejected() -> None:
    incoming = _rows(("r1", "A", 10), ("r3", "B", 31), ("r3", "B", 32))
    outcome = run_table_load([], incoming, primary_key=("record_id",), schema=SCHEMA)
    assert _reasons(outcome).count(REASON_DUPLICATE_PK) == 2
    assert _by_id(outcome) == {"r1": 10}  # both r3 rows rejected


def test_auto_delete_does_not_run_when_batch_has_rejects() -> None:
    existing = _rows(("r1", "A", 10), ("r2", "A", 20), ("r3", "B", 30))
    # Duplicate r3 + blank key create rejects; auto-delete must be suppressed.
    incoming = _rows(("r1", "A", 10), ("r2", "A", 22), ("r3", "B", 31), ("r3", "B", 32)) + [
        {"record_id": "", "group_id": "A", "amount": 5}
    ]
    outcome = run_table_load(
        existing, incoming, primary_key=("record_id",), schema=SCHEMA, auto_delete=True
    )
    assert outcome.auto_delete_ran is False
    assert outcome.deleted == 0
    result = _by_id(outcome)
    # Safe rows updated, r3 preserved from prior state (not deleted).
    assert result["r1"] == 10
    assert result["r2"] == 22
    assert result["r3"] == 30


def test_missing_schema_column_fails_load() -> None:
    incoming = [{"record_id": "r1", "group_id": "A"}]  # 'amount' missing
    with pytest.raises(LoadError, match="missing declared schema column"):
        run_table_load([], incoming, primary_key=("record_id",), schema=SCHEMA)


def test_extra_source_column_is_projected_away() -> None:
    incoming = [{"record_id": "r1", "group_id": "A", "amount": 10, "junk": "x"}]
    outcome = run_table_load([], incoming, primary_key=("record_id",), schema=SCHEMA)
    assert set(outcome.final_rows[0].keys()) == {"record_id", "group_id", "amount"}


def test_strict_extra_columns_fails() -> None:
    incoming = [{"record_id": "r1", "group_id": "A", "amount": 10, "junk": "x"}]
    with pytest.raises(LoadError, match="extra source column"):
        run_table_load(
            [], incoming, primary_key=("record_id",), schema=SCHEMA, strict_extra_columns=True
        )


def test_cast_failure_rejects_row() -> None:
    incoming = [
        {"record_id": "r1", "group_id": "A", "amount": 10},
        {"record_id": "r2", "group_id": "A", "amount": "not-a-number"},
    ]
    outcome = run_table_load([], incoming, primary_key=("record_id",), schema=SCHEMA)
    assert REASON_CAST in _reasons(outcome)
    assert _by_id(outcome) == {"r1": 10}


def test_casts_declared_types() -> None:
    incoming = [{"record_id": "r1", "group_id": "A", "amount": "42"}]
    outcome = run_table_load([], incoming, primary_key=("record_id",), schema=SCHEMA)
    assert outcome.final_rows[0]["amount"] == 42
    assert isinstance(outcome.final_rows[0]["amount"], int)


def test_three_run_behaviour_matches_fixture_expectations() -> None:
    """Mirror the plan's run 1/2/3 for auto-delete vs keep-missing tables."""

    run1 = _rows(("r1", "A", 10), ("r2", "A", 20), ("r3", "B", 30))
    run2 = _rows(("r1", "A", 10), ("r2", "A", 22), ("r3", "B", 31), ("r3", "B", 32)) + [
        {"record_id": "", "group_id": "A", "amount": 5}
    ]
    run3 = _rows(("r2", "A", 22), ("r3", "B", 33), ("r4", "B", 40))

    def load(existing, incoming, auto_delete):
        return run_table_load(
            existing, incoming, primary_key=("record_id",), schema=SCHEMA, auto_delete=auto_delete
        )

    # Auto-delete table.
    auto = load([], run1, True).final_rows
    out2 = load(auto, run2, True)
    assert out2.auto_delete_ran is False  # rejects present
    auto = out2.final_rows
    out3 = load(auto, run3, True)
    assert out3.auto_delete_ran is True
    assert set(row["record_id"] for row in out3.final_rows) == {"r2", "r3", "r4"}  # r1 removed

    # Keep-missing table, same inputs.
    keep = load([], run1, False).final_rows
    keep = load(keep, run2, False).final_rows
    keep = load(keep, run3, False).final_rows
    assert "r1" in {row["record_id"] for row in keep}  # r1 retained
