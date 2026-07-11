"""Common CRUD, step, and report models for load execution.

These models are object-kind neutral: a Folder step and a Table step share one
:class:`StepLog` shape and one :class:`CrudCounts` structure, differing only in
the unit of work (``files`` vs ``rows``) and their supplementary ``details``.
It also holds the shared authoring-contract helpers both kinds use: every
``read()`` returns the same ``(upserts, deletes, messages)`` triplet, and the
third item is a sequence of small structured messages.
The module imports nothing heavy so it is safe to import anywhere in the core.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from ..errors import LoadError

FILES = "files"
ROWS = "rows"

_UNIT_BY_KIND = {"Folder": FILES, "Table": ROWS}

# Supported supplementary-message levels. Errors are captured centrally from
# exceptions, never supplied as messages, so they are not a level here.
MESSAGE_LEVELS = ("info", "warning")
_MESSAGE_KEYS = frozenset({"level", "message", "fields"})


def require_triplet(result, kind: str) -> tuple:
    """Require an object ``read()`` to return exactly three values.

    Both kinds share the endpoint shape ``(upserts, deletes, messages)``; a bare
    return value (e.g. a DataFrame or a StagingFolder) or a wrong-arity tuple is
    rejected before any validation or mutation.
    """

    if not isinstance(result, tuple) or len(result) != 3:
        shape = f"a {len(result)}-tuple" if isinstance(result, tuple) else type(result).__name__
        raise LoadError(
            f"{kind}.read() must return exactly three values "
            f"(upserts, deletes, messages); got {shape}"
        )
    return result


def validate_messages(messages) -> tuple[dict, ...]:
    """Validate the supplementary-message sequence and return it normalised.

    Each message is a small stable mapping: a required ``level`` (one of
    :data:`MESSAGE_LEVELS`), a required non-empty ``message`` string, and an
    optional ``fields`` mapping for structured context. The whole message must
    be JSON-serialisable.
    """

    if isinstance(messages, (str, bytes, dict)):
        raise LoadError("messages must be a sequence of message mappings")
    try:
        items = list(messages)
    except TypeError as exc:
        raise LoadError("messages must be a sequence of message mappings") from exc

    validated: list[dict] = []
    for message in items:
        if not isinstance(message, dict):
            raise LoadError("each message must be a mapping")
        unexpected = set(message) - _MESSAGE_KEYS
        if unexpected:
            raise LoadError(
                "message keys must be a subset of {level, message, fields}; "
                f"unexpected {sorted(unexpected)}"
            )
        if message.get("level") not in MESSAGE_LEVELS:
            raise LoadError(
                f"message 'level' must be one of {MESSAGE_LEVELS}; got {message.get('level')!r}"
            )
        text = message.get("message")
        if not isinstance(text, str) or not text.strip():
            raise LoadError("message 'message' must be non-empty text")
        if "fields" in message and not isinstance(message["fields"], dict):
            raise LoadError("message 'fields' must be a mapping when present")
        try:
            json.dumps(message)
        except (TypeError, ValueError) as exc:
            raise LoadError("messages must be JSON-serialisable") from exc
        validated.append(dict(message))
    return tuple(validated)


def crud_unit_for_kind(kind: str) -> str:
    """Standard CRUD unit for an object kind (``files`` for Folder, ``rows`` for
    Table); empty for kinds that are not loaded."""

    return _UNIT_BY_KIND.get(kind, "")


@dataclass(frozen=True)
class CrudCounts:
    """Standard create/read/update/delete counts for one load step.

    ``unit`` names what is being counted (``files`` for Folder objects, ``rows``
    for Table objects). ``read`` is the total input the object produced; an input
    that leaves the destination unchanged contributes to ``read`` only.
    """

    unit: str
    read: int = 0
    created: int = 0
    updated: int = 0
    deleted: int = 0

    def to_dict(self) -> dict:
        return {
            "unit": self.unit,
            "read": self.read,
            "created": self.created,
            "updated": self.updated,
            "deleted": self.deleted,
        }


@dataclass
class StepLog:
    """Durable record for a single load step, shared by all object kinds."""

    workflow_id: str
    timestamp: str
    object_id: str
    module: str
    kind: str
    status: str
    crud: CrudCounts
    completed_at: str | None = None
    duration_ms: int | None = None
    messages: tuple[dict, ...] = ()
    details: dict = field(default_factory=dict)
    error: dict | None = None

    def to_dict(self) -> dict:
        return {
            "workflow_id": self.workflow_id,
            "timestamp": self.timestamp,
            "completed_at": self.completed_at,
            "duration_ms": self.duration_ms,
            "object_id": self.object_id,
            "module": self.module,
            "kind": self.kind,
            "status": self.status,
            "crud": self.crud.to_dict(),
            "messages": [dict(message) for message in self.messages],
            "details": dict(self.details),
            "error": self.error,
        }


def planned_step_log(object_id: str, kind: str, *, module: str = "") -> StepLog:
    """Build a not-yet-executed step record for planning (``--dry-run``) output."""

    return StepLog(
        workflow_id="",
        timestamp="",
        object_id=object_id,
        module=module,
        kind=kind,
        status="planned",
        crud=CrudCounts(unit=crud_unit_for_kind(kind)),
    )


@dataclass
class LoadReport:
    """Outcome of a load run."""

    runtime_root: str
    executed: bool
    workflow_id: str = ""
    log_dir: str = ""
    steps: tuple[StepLog, ...] = ()
    ok: bool = True
    message: str = ""

    def to_dict(self) -> dict:
        return {
            "workflow_id": self.workflow_id,
            "log_dir": self.log_dir,
            "runtime_root": self.runtime_root,
            "executed": self.executed,
            "ok": self.ok,
            "message": self.message,
            "steps": [step.to_dict() for step in self.steps],
        }
