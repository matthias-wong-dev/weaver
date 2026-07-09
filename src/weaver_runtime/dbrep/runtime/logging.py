"""Structured step logs for load execution."""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass
class StepLog:
    """Counts and status for a single load step."""

    object_id: str
    kind: str
    status: str = "pending"
    input: int = 0
    accepted: int = 0
    rejected: int = 0
    inserted: int = 0
    updated: int = 0
    deleted: int = 0
    auto_delete_ran: bool = False
    message: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class LoadReport:
    """Outcome of a load run."""

    runtime_root: str
    executed: bool
    steps: tuple[StepLog, ...] = ()
    ok: bool = True
    message: str = ""

    def to_dict(self) -> dict:
        return {
            "runtime_root": self.runtime_root,
            "executed": self.executed,
            "ok": self.ok,
            "message": self.message,
            "steps": [step.to_dict() for step in self.steps],
        }
