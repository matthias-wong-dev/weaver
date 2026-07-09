"""Governed table load policy (pure, no PySpark).

This is the source of truth for Weaver's governed write behaviour. It operates
on rows as plain dicts so it is fully unit-testable without Spark. The Spark
runtime projects and casts incoming frames to the declared schema before rows
enter this policy.

Behaviour summary:

* No primary key -> append-only.
* Blank primary key -> reject row.
* Duplicate incoming primary key -> reject the duplicated rows.
* Missing declared schema column -> fail load.
* Extra source column -> projected away (unless strict-extra-columns).
* Primary key + not auto-delete -> upsert; missing rows remain.
* Primary key + auto-delete -> upsert; delete missing rows, but only when the
  incoming batch has no rejects.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Sequence

from ..errors import LoadError
from ..ses.metadata import APPEND, UPSERT

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
    auto_delete_ran: bool
    columns: tuple[str, ...] = ()

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
    auto_delete: bool = False,
    load_mode: str | None = None,
    strict_extra_columns: bool = False,
) -> LoadOutcome:
    """Apply the governed load policy and return the resulting table state."""

    primary_key = tuple(primary_key)
    input_count = len(incoming_rows)

    projected, columns = _apply_schema(
        incoming_rows, tuple(schema), primary_key, strict_extra_columns
    )

    if primary_key:
        accepted, pk_rejects = _validate_primary_key(projected, primary_key)
    else:
        accepted, pk_rejects = list(projected), []

    rejected = pk_rejects
    has_rejects = bool(rejected)

    mode = (load_mode or (UPSERT if primary_key else APPEND)).lower()
    effective_auto_delete = auto_delete and not has_rejects and mode == UPSERT and bool(primary_key)

    final, inserted, updated, deleted = _plan_write(
        list(existing_rows), accepted, primary_key, mode, effective_auto_delete
    )

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
        auto_delete_ran=effective_auto_delete and deleted >= 0,
        columns=columns,
    )


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

    counts: dict = {}
    for row in non_blank:
        key = _key(row, primary_key)
        counts[key] = counts.get(key, 0) + 1

    accepted: list[dict] = []
    for row in non_blank:
        if counts[_key(row, primary_key)] > 1:
            rejects.append(_reject(row, REASON_DUPLICATE_PK))
        else:
            accepted.append(row)
    return accepted, rejects


def _plan_write(existing, accepted, primary_key, mode, effective_auto_delete):
    if not primary_key or mode == APPEND:
        final = list(existing) + list(accepted)
        return final, len(accepted), 0, 0

    existing_by_key = {_key(row, primary_key): row for row in existing}
    incoming_by_key = {_key(row, primary_key): row for row in accepted}

    existing_keys = set(existing_by_key)
    incoming_keys = set(incoming_by_key)

    inserted = len(incoming_keys - existing_keys)
    updated = len(incoming_keys & existing_keys)

    if effective_auto_delete:
        deleted = len(existing_keys - incoming_keys)
        final = list(incoming_by_key.values())
    else:
        deleted = 0
        kept = [existing_by_key[key] for key in existing_keys - incoming_keys]
        final = list(incoming_by_key.values()) + kept

    return final, inserted, updated, deleted


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
