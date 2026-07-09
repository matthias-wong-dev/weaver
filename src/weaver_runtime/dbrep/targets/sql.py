"""SQL target adapter: plan schema/table/view and stored-procedure installs.

Plan-only. Real SQL DDL and stored-procedure execution are implemented against a
live endpoint (see the SQL runtime notes); the build model records intended
operations, including honouring ``degrees_of_parallelism`` at load time.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from ..config.databases import SQL
from ..ses.metadata import VIEW
from .base import InstallAction, TargetAdapter

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..build.planner import PlannedObject

#: Schema-qualified name of the installed load stored procedure (underscore
#: schema so runtime discovery ignores it, like other ``_weaver`` artifacts).
SQL_LOAD_PROCEDURE = "_weaver.load"


class SqlTarget(TargetAdapter):
    """Describes SQL schema/table/view + load stored-procedure installs."""

    type = SQL
    plan_only = True

    def plan(self, planned: "PlannedObject", host_root: Path | None) -> InstallAction:
        self.validate_kind(planned.kind)
        operations = ["create schema"]
        if planned.kind == VIEW:
            operations.append("create or replace view")
        else:
            operations.append("create or update table")
            operations.append("install load stored procedure")
            operations.append("install manifest/metadata tables")
        return InstallAction(
            id=planned.id,
            kind=planned.kind,
            target_type=self.type,
            materialisation=planned.materialisation,
            absolute_path=None,
            operations=tuple(operations),
            applied=False,
        )
