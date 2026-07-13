"""Governed table load policy (pure, no PySpark).

This is the source of truth for Weaver's governed write behaviour. It operates
on rows as plain dicts so it is fully unit-testable without Spark. The Spark
runtime projects and casts incoming frames to the declared schema before rows
enter this policy.

Behaviour summary:

* No primary key -> full replacement.
* Blank primary key -> reject row.
* Duplicate incoming primary key -> accept one unspecified row and reject surplus rows.
* Missing declared schema column -> fail load.
* Extra source column -> projected away (unless strict-extra-columns).
* Primary key + incremental -> upsert; missing rows remain.
* Primary key + non-incremental -> upsert and delete missing rows.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Sequence

from ..errors import LoadError
from ..ses.metadata import APPEND, REPLACE, UPSERT

REASON_MISSING_COLUMN = "missing_schema_column"
REASON_BLANK_PK = "blank_primary_key"
REASON_DUPLICATE_PK = "duplicate_primary_key"

REJECT_REASON_KEY = "_reject_reason"


@dataclass
class LoadOutcome:
    """Result of applying the load policy for one table step."""

    final_rows: list[dict]
    accepted: list[dict]
    rejected: list[dict]
    input_count: int
    accepted_count: int
    rejected_count: int
    inserted: int
    updated: int
    deleted: int
    reconciliation_ran: bool
    columns: tuple[str, ...] = ()
    explicit_delete_keys_read: int = 0
    explicit_delete_keys_matched: int = 0
    explicit_delete_keys_unmatched: int = 0

    def counts(self) -> dict:
        return {
            "input": self.input_count,
            "accepted": self.accepted_count,
            "rejected": self.rejected_count,
            "inserted": self.inserted,
            "updated": self.updated,
            "deleted": self.deleted,
        }


def run_table_load(
    existing_rows: Sequence[dict],
    incoming_rows: Sequence[dict],
    *,
    primary_key: Sequence[str] = (),
    schema: Sequence[tuple[str, str]] = (),
    is_incremental: bool = False,
    load_mode: str | None = None,
    strict_extra_columns: bool = False,
    explicit_delete_keys: Sequence[Sequence] = (),
    object_name: str = "",
) -> LoadOutcome:
    """Apply the governed load policy and return the resulting table state.

    ``explicit_delete_keys`` are primary-key tuples (in declared key order) whose
    existing rows should be removed. Deletion has exactly one authority: a table
    without a primary key cannot delete rows, and complete reconciliation and
    explicit deletes cannot be combined. All authority and key validation happens before
    any write is planned.
    """

    primary_key = tuple(primary_key)
    input_count = len(incoming_rows)

    if isinstance(explicit_delete_keys, (str, bytes)):
        raise LoadError("explicit delete keys must be a sequence of primary-key tuples")
    explicit_provided = tuple(explicit_delete_keys or ())
    _validate_delete_authority(primary_key, is_incremental, explicit_provided, object_name)

    projected, columns = _apply_schema(
        incoming_rows, tuple(schema), primary_key, strict_extra_columns
    )

    if primary_key:
        accepted, pk_rejects = _validate_primary_key(projected, primary_key)
    else:
        accepted, pk_rejects = list(projected), []

    rejected = pk_rejects
    mode = (load_mode or (UPSERT if primary_key else REPLACE)).lower()
    is_complete_result = not is_incremental
    reconciliation_ran = is_complete_result and mode == UPSERT and bool(primary_key)

    explicit_keys: tuple[tuple, ...] = ()
    if primary_key and is_incremental and explicit_provided:
        explicit_keys = _normalise_delete_keys(explicit_provided, primary_key, object_name)

    final, inserted, updated, deleted, explicit_detail = _plan_write(
        list(existing_rows), accepted, primary_key, mode, reconciliation_ran, explicit_keys
    )
    explicit_read, explicit_matched, explicit_unmatched = explicit_detail

    return LoadOutcome(
        final_rows=final,
        accepted=accepted,
        rejected=rejected,
        input_count=input_count,
        accepted_count=len(accepted),
        rejected_count=len(rejected),
        inserted=inserted,
        updated=updated,
        deleted=deleted,
        reconciliation_ran=reconciliation_ran,
        columns=columns,
        explicit_delete_keys_read=explicit_read,
        explicit_delete_keys_matched=explicit_matched,
        explicit_delete_keys_unmatched=explicit_unmatched,
    )


def _validate_delete_authority(primary_key, is_incremental, explicit_provided, object_name) -> None:
    """Enforce the single-deletion-authority rules before any write is planned."""

    label = f"Table {object_name}" if object_name else "Table"
    if not primary_key:
        if explicit_provided:
            raise LoadError(
                f"{label} returned explicit deletions, but no primary key is declared. "
                "Explicit row deletion requires a declared primary key."
            )
        if is_incremental:
            raise LoadError(
                f"{label} is incremental, but no primary key is declared. "
                "Incremental table loading requires a declared primary key."
            )
    elif not is_incremental and explicit_provided:
        raise LoadError(
            f"{label} is non-incremental and also returned explicit deletions. "
            "A table must use either complete reconciliation or explicit deletion, not both."
        )


def _normalise_delete_keys(explicit_delete_keys, primary_key, object_name) -> tuple[tuple, ...]:
    """Validate explicit delete tuples and deduplicate identical ones.

    Duplicate identical tuples are collapsed to one (the documented rule).
    """

    label = f"Table {object_name}" if object_name else "Table"
    expected = len(primary_key)
    normalised: list[tuple] = []
    seen: set[tuple] = set()
    for entry in explicit_delete_keys:
        if not isinstance(entry, tuple):
            raise LoadError(
                f"{label} explicit delete keys must be tuples of primary-key values; "
                f"got {entry!r}"
            )
        if len(entry) != expected:
            raise LoadError(
                f"{label} explicit delete tuple {entry!r} has {len(entry)} value(s); "
                f"expected {expected} to match the primary key {primary_key}"
            )
        if any(value is None for value in entry):
            raise LoadError(
                f"{label} explicit delete tuple {entry!r} contains a null primary-key value"
            )
        if entry not in seen:
            seen.add(entry)
            normalised.append(entry)
    return tuple(normalised)


def _apply_schema(rows, schema, primary_key, strict_extra_columns):
    if not schema:
        columns = tuple(rows[0].keys()) if rows else ()
        return list(rows), columns

    declared = [column for column, _ in schema]
    declared_set = set(declared)

    missing_pk = [column for column in primary_key if column not in declared_set]
    if missing_pk:
        raise LoadError(
            "primary key columns are not part of the declared schema: "
            + ", ".join(missing_pk)
        )

    provided = set()
    for row in rows:
        provided.update(row.keys())
    if rows:
        missing = declared_set - provided
        if missing:
            raise LoadError(
                "missing declared schema column(s): " + ", ".join(sorted(missing))
            )
        if strict_extra_columns:
            extra = provided - declared_set
            if extra:
                raise LoadError(
                    "unexpected extra source column(s): " + ", ".join(sorted(extra))
                )

    projected = [{column: row.get(column) for column in declared} for row in rows]
    return projected, tuple(declared)


def _validate_primary_key(rows, primary_key):
    non_blank: list[dict] = []
    rejects: list[dict] = []
    for row in rows:
        if _is_blank_key(row, primary_key):
            rejects.append(_reject(row, REASON_BLANK_PK))
        else:
            non_blank.append(row)

    accepted: list[dict] = []
    seen: set[tuple] = set()
    for row in non_blank:
        key = _key(row, primary_key)
        if key in seen:
            rejects.append(_reject(row, REASON_DUPLICATE_PK))
        else:
            seen.add(key)
            accepted.append(row)
    return accepted, rejects


def _plan_write(existing, accepted, primary_key, mode, reconciliation_ran, explicit_keys):
    no_explicit = (0, 0, 0)
    if not primary_key:
        return list(accepted), len(accepted), 0, len(existing), no_explicit
    if mode == APPEND:
        final = list(existing) + list(accepted)
        return final, len(accepted), 0, 0, no_explicit
    if mode == REPLACE:
        return list(accepted), len(accepted), 0, len(existing), no_explicit

    existing_by_key = {_key(row, primary_key): row for row in existing}
    incoming_by_key = {_key(row, primary_key): row for row in accepted}

    existing_keys = set(existing_by_key)
    incoming_keys = set(incoming_by_key)

    inserted = len(incoming_keys - existing_keys)
    updated = len(incoming_keys & existing_keys)

    if reconciliation_ran:
        deleted = len(existing_keys - incoming_keys)
        final = list(incoming_by_key.values())
        return final, inserted, updated, deleted, no_explicit

    if explicit_keys:
        # Upserts are authoritative: a key that is both staged and explicitly
        # deleted is upserted, and its delete is counted unmatched.
        delete_set = set(explicit_keys)
        removable = {
            key for key in existing_keys if key in delete_set and key not in incoming_keys
        }
        deleted = len(removable)
        kept = [
            existing_by_key[key]
            for key in existing_keys
            if key not in incoming_keys and key not in removable
        ]
        final = list(incoming_by_key.values()) + kept
        read = len(explicit_keys)
        return final, inserted, updated, deleted, (read, deleted, read - deleted)

    deleted = 0
    kept = [existing_by_key[key] for key in existing_keys - incoming_keys]
    final = list(incoming_by_key.values()) + kept
    return final, inserted, updated, deleted, no_explicit


def _is_blank_key(row: dict, primary_key: Iterable[str]) -> bool:
    for column in primary_key:
        value = row.get(column)
        if value is None:
            return True
        if isinstance(value, str) and value.strip() == "":
            return True
    return False


def _key(row: dict, primary_key: Iterable[str]) -> tuple:
    return tuple(row.get(column) for column in primary_key)


def _reject(row: dict, reason: str) -> dict:
    rejected = dict(row)
    rejected[REJECT_REASON_KEY] = reason
    return rejected
