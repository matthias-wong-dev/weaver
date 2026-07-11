"""Durable workflow and step logging for load execution.

Every ``weaver load`` invocation is one *workflow*: it mints a workflow id,
creates a single log directory under ``Files/_logs/<workflow_id>/``, and writes
one JSON record per executed object step into it. Records are written the moment
a step finishes — success or failure — so a run that fails partway still leaves
the completed steps and the full structured exception on disk.

The structured exception capture is the strong pattern ported from the legacy
ILG lakehouse logger: it keeps Spark's error class, SQL state, message
parameters, and the underlying Java exception text where the runtime exposes
them, alongside the Python type, repr, args, traceback, cause, and context.

This module is pure filesystem + stdlib so it imports nowhere near PySpark.
"""

from __future__ import annotations

import json
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

LOGS_RELATIVE_ROOT = "Files/_logs"


def utc_now() -> datetime:
    """Current time as a timezone-aware UTC ``datetime``."""

    return datetime.now(timezone.utc)


def utc_timestamp(moment: datetime | None = None) -> str:
    """ISO-8601 UTC timestamp, e.g. ``2026-07-11T07:24:31.104281+00:00``."""

    return (moment or utc_now()).isoformat()


def utc_compact(moment: datetime | None = None) -> str:
    """Compact UTC stamp safe for identifiers, e.g. ``20260711T072431Z``."""

    return (moment or utc_now()).strftime("%Y%m%dT%H%M%SZ")


def duration_ms(started: datetime, completed: datetime) -> int:
    """Whole-millisecond duration between two instants."""

    return int((completed - started).total_seconds() * 1000)


def create_workflow_id(moment: datetime | None = None) -> str:
    """Mint one ``{timestamp}_{uuid}`` id for a load invocation.

    Example: ``20260711T072431Z_a13f92``.
    """

    return f"{utc_compact(moment)}_{uuid.uuid4().hex[:6]}"


def create_workflow_log_dir(lakehouse_root: Path, workflow_id: str) -> Path:
    """Create and return ``<lakehouse>/Files/_logs/<workflow_id>/``."""

    log_dir = Path(lakehouse_root) / LOGS_RELATIVE_ROOT / workflow_id
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir


def _step_filename(step) -> str:
    """``{timestamp}_{uuid}.json`` — object/module names never appear here.

    The timestamp mirrors the step's own start instant when available so the
    filename sorts in execution order; a fresh uuid keeps it unique regardless.
    """

    stamp = None
    if getattr(step, "timestamp", ""):
        try:
            stamp = datetime.fromisoformat(step.timestamp).strftime("%Y%m%dT%H%M%S.%fZ")
        except ValueError:
            stamp = None
    if stamp is None:
        stamp = utc_now().strftime("%Y%m%dT%H%M%S.%fZ")
    return f"{stamp}_{uuid.uuid4().hex[:6]}.json"


def write_step_log(log_dir: Path, step) -> Path:
    """Write one step record as an indented JSON file and return its path.

    The record carries the object id, module, and kind inside the JSON; the
    filename is only ``{timestamp}_{uuid}.json``.
    """

    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / _step_filename(step)
    path.write_text(
        json.dumps(step.to_dict(), indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    return path


def exception_detail(exc: BaseException) -> dict[str, Any]:
    """Return a structured, high-signal exception payload.

    Captures the exception module/class, repr, message, args, and full
    traceback, plus Spark/JVM diagnostics (error class, SQL state, message
    parameters, Java exception text) and the cause/context chain where present.
    """

    detail: dict[str, Any] = {
        "type": f"{exc.__class__.__module__}.{exc.__class__.__name__}",
        "repr": repr(exc),
        "message": str(exc),
        "args": [str(arg) for arg in getattr(exc, "args", [])],
        "traceback": traceback.format_exception(type(exc), exc, exc.__traceback__),
    }

    for attr in ("desc", "_desc", "error_class", "sql_state"):
        if hasattr(exc, attr):
            try:
                detail[attr] = getattr(exc, attr)
            except Exception:
                pass

    for method_name, key in (
        ("getErrorClass", "error_class"),
        ("getSqlState", "sql_state"),
        ("getMessageParameters", "message_parameters"),
    ):
        method = getattr(exc, method_name, None)
        if callable(method):
            try:
                detail[key] = method()
            except Exception:
                pass

    java_exception = getattr(exc, "java_exception", None)
    if java_exception is not None:
        try:
            detail["java_exception"] = java_exception.toString()
        except Exception:
            pass

    cause = getattr(exc, "__cause__", None)
    if cause is not None:
        detail["cause"] = {
            "type": f"{cause.__class__.__module__}.{cause.__class__.__name__}",
            "repr": repr(cause),
            "message": str(cause),
        }

    context = getattr(exc, "__context__", None)
    if context is not None and context is not cause:
        detail["context"] = {
            "type": f"{context.__class__.__module__}.{context.__class__.__name__}",
            "repr": repr(context),
            "message": str(context),
        }

    return detail
