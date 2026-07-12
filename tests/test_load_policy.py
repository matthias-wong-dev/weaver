from __future__ import annotations

import pytest

from weaver_runtime.dbrep.errors import LoadError
from weaver_runtime.dbrep.runtime.load_policy import (
    REASON_BLANK_PK,
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


def test_no_primary_key_replaces_existing_rows() -> None:
    existing = _rows(("r1", "A", 10))
    incoming = _rows(("r1", "A", 99), ("r2", "A", 20))
    outcome = run_table_load(existing, incoming, primary_key=(), schema=SCHEMA)
    assert outcome.final_rows == incoming
    assert outcome.inserted == 2
    assert outcome.updated == 0
    assert outcome.deleted == 1


def test_empty_no_primary_key_load_empties_table() -> None:
    existing = _rows(("r1", "A", 10), ("r2", "A", 20))
    outcome = run_table_load(existing, [], primary_key=(), schema=SCHEMA)
    assert outcome.final_rows == []
    assert (outcome.inserted, outcome.updated, outcome.deleted) == (0, 0, 2)


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


def test_declared_schema_projects_without_casting() -> None:
    incoming = [{"record_id": "r1", "group_id": "A", "amount": "42"}]
    outcome = run_table_load([], incoming, primary_key=("record_id",), schema=SCHEMA)
    assert outcome.final_rows[0]["amount"] == "42"


COMPOSITE_SCHEMA = (("agency", "string"), ("period", "string"), ("amount", "int"))


def test_explicit_single_key_delete_removes_only_named_row() -> None:
    existing = _rows(("r1", "A", 10), ("r2", "A", 20), ("r3", "B", 30))
    incoming = _rows(("r2", "A", 22))
    outcome = run_table_load(
        existing,
        incoming,
        primary_key=("record_id",),
        schema=SCHEMA,
        auto_delete=False,
        explicit_delete_keys=(("r1",),),
    )
    assert _by_id(outcome) == {"r2": 22, "r3": 30}  # r1 deleted, r3 kept
    assert outcome.deleted == 1
    assert outcome.auto_delete_ran is False
    assert (
        outcome.explicit_delete_keys_read,
        outcome.explicit_delete_keys_matched,
        outcome.explicit_delete_keys_unmatched,
    ) == (1, 1, 0)


def test_explicit_composite_key_delete_follows_declared_order() -> None:
    existing = [
        {"agency": "a", "period": "2026-07", "amount": 1},
        {"agency": "b", "period": "2026-06", "amount": 2},
        {"agency": "c", "period": "2026-05", "amount": 3},
    ]
    incoming = [{"agency": "c", "period": "2026-05", "amount": 30}]
    outcome = run_table_load(
        existing,
        incoming,
        primary_key=("agency", "period"),
        schema=COMPOSITE_SCHEMA,
        auto_delete=False,
        explicit_delete_keys=(("a", "2026-07"), ("b", "2026-06")),
    )
    assert {(r["agency"], r["period"]) for r in outcome.final_rows} == {("c", "2026-05")}
    assert outcome.deleted == 2
    assert outcome.explicit_delete_keys_matched == 2


def test_unmatched_explicit_delete_does_not_increment_deleted() -> None:
    existing = _rows(("r1", "A", 10))
    outcome = run_table_load(
        existing,
        [],
        primary_key=("record_id",),
        schema=SCHEMA,
        explicit_delete_keys=(("nope",),),
    )
    assert outcome.deleted == 0
    assert _by_id(outcome) == {"r1": 10}
    assert (
        outcome.explicit_delete_keys_read,
        outcome.explicit_delete_keys_matched,
        outcome.explicit_delete_keys_unmatched,
    ) == (1, 0, 1)


def test_duplicate_explicit_delete_tuples_deduplicated() -> None:
    existing = _rows(("r1", "A", 10), ("r2", "A", 20))
    outcome = run_table_load(
        existing,
        [],
        primary_key=("record_id",),
        schema=SCHEMA,
        explicit_delete_keys=(("r1",), ("r1",)),
    )
    assert outcome.deleted == 1
    assert outcome.explicit_delete_keys_read == 1  # collapsed to one unique key
    assert _by_id(outcome) == {"r2": 20}


def test_key_both_staged_and_explicitly_deleted_is_upserted() -> None:
    outcome = run_table_load(
        _rows(("r1", "A", 10)),
        _rows(("r1", "A", 99)),
        primary_key=("record_id",),
        schema=SCHEMA,
        explicit_delete_keys=(("r1",),),
    )
    assert _by_id(outcome) == {"r1": 99}  # upsert wins over the delete
    assert outcome.deleted == 0
    assert outcome.explicit_delete_keys_unmatched == 1


def test_null_value_in_delete_tuple_is_rejected() -> None:
    with pytest.raises(LoadError, match="null primary-key value"):
        run_table_load(
            [], [], primary_key=("record_id",), schema=SCHEMA, explicit_delete_keys=((None,),)
        )


def test_wrong_delete_tuple_arity_is_rejected() -> None:
    with pytest.raises(LoadError, match="expected 1"):
        run_table_load(
            [],
            [],
            primary_key=("record_id",),
            schema=SCHEMA,
            explicit_delete_keys=(("r1", "extra"),),
        )


def test_non_tuple_delete_entry_is_rejected() -> None:
    with pytest.raises(LoadError, match="tuples of primary-key values"):
        run_table_load(
            [], [], primary_key=("record_id",), schema=SCHEMA, explicit_delete_keys=("r1",)
        )


def test_explicit_delete_without_primary_key_fails() -> None:
    with pytest.raises(LoadError, match="Explicit row deletion requires a declared primary key"):
        run_table_load(
            [],
            _rows(("r1", "A", 10)),
            primary_key=(),
            schema=SCHEMA,
            explicit_delete_keys=(("r1",),),
            object_name="Sales.CustomerOrder",
        )


def test_auto_delete_without_primary_key_fails() -> None:
    with pytest.raises(LoadError, match="Automatic row deletion requires a declared primary key"):
        run_table_load(
            [],
            [],
            primary_key=(),
            schema=SCHEMA,
            auto_delete=True,
            object_name="Sales.CustomerOrder",
        )


def test_auto_delete_with_explicit_deletes_fails() -> None:
    with pytest.raises(LoadError, match="either automatic deletion or explicit deletion, not both"):
        run_table_load(
            [],
            [],
            primary_key=("record_id",),
            schema=SCHEMA,
            auto_delete=True,
            explicit_delete_keys=(("r1",),),
            object_name="Sales.CustomerOrder",
        )


def test_authority_error_names_the_table() -> None:
    with pytest.raises(LoadError, match="Table Sales.CustomerOrder"):
        run_table_load(
            [], [], primary_key=(), schema=SCHEMA, auto_delete=True, object_name="Sales.CustomerOrder"
        )


def test_auto_delete_with_empty_explicit_still_auto_deletes() -> None:
    existing = _rows(("r1", "A", 10), ("r2", "A", 20))
    incoming = _rows(("r2", "A", 22))
    outcome = run_table_load(
        existing,
        incoming,
        primary_key=("record_id",),
        schema=SCHEMA,
        auto_delete=True,
        explicit_delete_keys=(),
    )
    assert outcome.auto_delete_ran is True
    assert outcome.deleted == 1
    assert _by_id(outcome) == {"r2": 22}


def test_no_pk_no_auto_empty_explicit_is_replacement() -> None:
    outcome = run_table_load(
        _rows(("r1", "A", 10)),
        _rows(("r2", "A", 20)),
        primary_key=(),
        schema=SCHEMA,
        explicit_delete_keys=(),
    )
    assert outcome.deleted == 1
    assert [row["record_id"] for row in outcome.final_rows] == ["r2"]


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
