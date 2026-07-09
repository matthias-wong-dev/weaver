"""Governed table load policy (pure, no PySpark).

This is the source of truth for Weaver's load behaviour. It operates on rows as
plain dicts so it is fully unit-testable without Spark; the Spark engine collects
the incoming frame, applies this policy against the existing table rows, and
writes the result back as Delta.

Behaviour summary:

* No primary key -> append-only.
* Blank primary key -> reject row.
* Duplicate incoming primary key -> reject the duplicated rows.
* Missing declared schema column -> fail load.
* Extra source column -> projected away (unless strict-extra-columns).
* Cast failure -> reject row.
* Primary key + not auto-delete -> upsert; missing rows remain.
* Primary key + auto-delete -> upsert; delete missing rows, but only when the
  incoming batch has no rejects.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone
from decimal import Decimal
from typing import Any, Iterable, Sequence

from ..errors import LoadError
from ..ses.metadata import APPEND, UPSERT

REASON_MISSING_COLUMN = "missing_schema_column"
REASON_CAST = "cast_failure"
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

    projected, cast_rejects, columns = _apply_schema(
        incoming_rows, tuple(schema), primary_key, strict_extra_columns
    )

    if primary_key:
        accepted, pk_rejects = _validate_primary_key(projected, primary_key)
    else:
        accepted, pk_rejects = list(projected), []

    rejected = cast_rejects + pk_rejects
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
        return list(rows), [], columns

    declared = [column for column, _ in schema]
    declared_set = set(declared)
    unknown_types = [type_name for _, type_name in schema if not _is_supported_type(type_name)]
    if unknown_types:
        raise LoadError(
            "unknown declared schema type(s): " + ", ".join(sorted(set(unknown_types)))
        )

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

    good: list[dict] = []
    rejects: list[dict] = []
    for row in rows:
        try:
            good.append({column: _cast(row.get(column), type_name) for column, type_name in schema})
        except (ValueError, TypeError):
            rejects.append(_reject(row, REASON_CAST))
    return good, rejects, tuple(declared)


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


_DECIMAL_RE = re.compile(r"^(decimal|numeric)\s*(?:\(\s*\d+\s*,\s*\d+\s*\))?$")


def _is_supported_type(type_name: str) -> bool:
    name = type_name.strip().lower()
    return (
        name in ("string", "str", "varchar", "text", "char")
        or name in ("int", "integer", "long", "bigint", "smallint", "tinyint")
        or name in ("double", "float", "real")
        or name in ("bool", "boolean")
        or name in ("date", "timestamp")
        or bool(_DECIMAL_RE.match(name))
    )


def _cast(value: Any, type_name: str) -> Any:
    if value is None:
        return None
    name = type_name.strip().lower()
    if name in ("string", "str", "varchar", "text", "char"):
        return str(value)
    if name in ("int", "integer", "long", "bigint", "smallint", "tinyint"):
        if isinstance(value, bool):
            raise ValueError("bool is not an integer")
        return int(value)
    if name in ("double", "float", "real"):
        if isinstance(value, bool):
            raise ValueError("bool is not numeric")
        return float(value)
    if _DECIMAL_RE.match(name):
        if isinstance(value, bool):
            raise ValueError("bool is not decimal")
        return value if isinstance(value, Decimal) else Decimal(str(value))
    if name in ("bool", "boolean"):
        if isinstance(value, bool):
            return value
        token = str(value).strip().lower()
        if token in ("true", "1"):
            return True
        if token in ("false", "0"):
            return False
        raise ValueError(f"not a boolean: {value!r}")
    if name == "date":
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        return date.fromisoformat(str(value).strip()[:10])
    if name == "timestamp":
        if isinstance(value, datetime):
            return value
        if isinstance(value, date):
            return datetime.combine(value, time.min)
        token = str(value).strip()
        parsed = datetime.fromisoformat(token.replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
        return parsed
    raise ValueError(f"unknown schema type: {type_name!r}")


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
