"""Render complete, executable Weaver Spark programs for a Lakehouse host.

A rendered program is one deterministic Python string that both execution paths
run verbatim:

* locally through ``execution.execute_program_local`` (``exec``);
* on Fabric through ``fabric.livy.run_runtime_program`` (Livy bootstrap → ``exec``).

Every program runs against the standard globals ``spark``, ``WEAVER_RUNTIME_ROOT``
and ``WEAVER_SPARK_ROOT`` and assigns a JSON-serialisable ``WEAVER_RESULT``. The
program is fully populated and operation-specific; the transport/execution
substrate stays generic and knows nothing about what the program does.

Rendering derives Delta specs and schemas only from the plan's SES metadata and
never emits environment-specific values (Fabric IDs, mount names, local absolute
roots, timestamps), so the output is deterministic and identical across paths.
"""

from __future__ import annotations

import json

from ..runtime.initialise import delta_specs_from_plan, validate_delta_specs

_BUILD_PROGRAM = '''\
import json

from weaver_runtime.dbrep.runtime.initialise import initialise_delta_tables

_SPECS = json.loads({specs!r})

_report = initialise_delta_tables(
    _SPECS,
    spark=spark,
    spark_root=WEAVER_SPARK_ROOT,
)

WEAVER_RESULT = _report.to_dict()
'''

_LOAD_PROGRAM = '''\
from weaver_runtime.dbrep.runtime.orchestrator import load_target_runtime

_report = load_target_runtime(
    WEAVER_RUNTIME_ROOT,
    execute=True,
    object_filter={object_filter!r},
    target_filter={target_filter!r},
    include_static={include_static!r},
    strict={strict!r},
    spark=spark,
    spark_root=WEAVER_SPARK_ROOT,
)

WEAVER_RESULT = _report.to_dict()
'''


def render_build_program(objects) -> str:
    """Render the Delta-initialisation build program for one Lakehouse host.

    ``objects`` are the host's planned objects in deterministic plan order. Only
    Delta ``Table`` objects become table specs; a missing schema raises before a
    program (and therefore before any deployment side effect) is produced.
    """

    specs = delta_specs_from_plan(objects)
    validate_delta_specs(specs)
    return _BUILD_PROGRAM.format(specs=json.dumps(specs))


def render_load_program(
    *,
    target_filter,
    object_filter,
    include_static: bool,
    strict: bool,
) -> str:
    """Render the target-only load program for a Lakehouse load invocation."""

    return _LOAD_PROGRAM.format(
        object_filter=tuple(object_filter) if object_filter else None,
        target_filter=target_filter,
        include_static=bool(include_static),
        strict=bool(strict),
    )
