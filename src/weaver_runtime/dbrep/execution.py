"""Generic local executor for rendered Weaver Spark programs.

This is the local twin of the Fabric Livy runtime submitter
(``weaver_runtime.fabric.livy.run_runtime_program``): both run an arbitrary
generated Weaver program string verbatim against the standard globals and return
its ``WEAVER_RESULT``. It knows nothing about whether the program is a build, a
load, or any future operation.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from .errors import ProgramError

RESULT_GLOBAL = "WEAVER_RESULT"


def execute_program_local(program: str, *, spark, runtime_root, spark_root) -> dict:
    """Execute a generated Weaver program string in an isolated scope.

    Adds ``<runtime_root>/_orchestrator`` to ``sys.path`` (so the program can
    import the bundled runtime), runs the exact supplied string, requires it to
    set a JSON-serialisable ``WEAVER_RESULT``, and restores ``sys.path``.

    Parity note: the guarantee is that the exact same generated program string
    runs locally and on Fabric, and that the staged ``_orchestrator`` bundle is a
    byte-for-byte copy of the live runtime source (``install_build`` copies it;
    a test asserts the hashes match). Because this runs in the CLI's own process,
    ``weaver_runtime`` is usually already imported, so the program binds the live
    package rather than the staged copy — but that copy is identical source, so
    package-level import identity is deliberately not pursued here.
    """

    runtime_root = Path(runtime_root)
    orchestrator = str(runtime_root / "_orchestrator")
    inserted = orchestrator not in sys.path
    if inserted:
        sys.path.insert(0, orchestrator)

    scope = {
        "spark": spark,
        "WEAVER_RUNTIME_ROOT": str(runtime_root),
        "WEAVER_SPARK_ROOT": str(spark_root),
    }
    try:
        exec(compile(program, "<weaver-program>", "exec"), scope)
        if RESULT_GLOBAL not in scope:
            raise ProgramError(f"generated program did not set {RESULT_GLOBAL}")
        result = scope[RESULT_GLOBAL]
        validate_program_result(result)
        return result
    finally:
        if inserted:
            try:
                sys.path.remove(orchestrator)
            except ValueError:  # pragma: no cover - already removed
                pass


def validate_program_result(result) -> None:
    """Require a program result to be JSON-serialisable."""

    try:
        json.dumps(result)
    except (TypeError, ValueError) as exc:
        raise ProgramError(
            f"generated program {RESULT_GLOBAL} is not JSON-serialisable"
        ) from exc
