"""Build-time Delta initialiser.

Materialises missing zero-row Delta tables for the declared ``Table`` objects.
Its work list and schemas come straight from the SES metadata carried on the
build plan (via :func:`delta_specs_from_plan`) — never from source data and never
by running an object's ``read()`` / ``load()``. The schema is taken only from the
declared metadata.

A Delta table is described by a small, JSON-serialisable spec
(``id`` / ``materialisation`` / ``schema``). The specs are rendered into the
shared generated build program (see ``dbrep.lakehouse.programs``), so the exact
same specs and the exact same materialisation function run locally (``exec``) and
on Fabric (Livy). Build success is recorded separately, after the program runs,
as ``build_complete.json``; this module does not write it. PySpark and Delta are
imported lazily through the shared helpers in ``.load``, so importing this module
stays free of Spark and the same schema conversion is used at build and at load.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config.databases import DELTA
from ..errors import BuildError
from ..ses.metadata import TABLE


@dataclass
class InitialiseReport:
    """Outcome of a build-time Delta initialisation against one root."""

    root: str
    created: tuple[str, ...] = ()
    existing: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "root": self.root,
            "created": list(self.created),
            "existing": list(self.existing),
        }


def delta_specs_from_plan(objects) -> list[dict]:
    """Delta table specs drawn straight from the planned objects' SES metadata.

    Pure: reads nothing from disk and never touches the installed catalogue.
    Each spec is ``{"id", "materialisation", "schema"}`` where ``schema`` is the
    declared ``[column, type]`` pairs (possibly empty when none was declared).
    """

    specs: list[dict] = []
    for planned in objects:
        if planned.target_type != DELTA or planned.kind != TABLE:
            continue
        specs.append(
            {
                "id": planned.id,
                "materialisation": planned.materialisation,
                "schema": [[column, type_name] for column, type_name in planned.source.metadata.schema],
            }
        )
    return specs


def validate_delta_specs(specs) -> None:
    """Fail if any Delta table spec has no declared schema.

    Callable during program generation, before any upload or table creation, so
    a missing schema fails the build with a clear error naming every offending
    object before any deployment side effect.
    """

    missing = sorted(spec["id"] for spec in specs if not spec.get("schema"))
    if missing:
        raise BuildError(
            "build-time Delta initialisation requires a declared schema; the "
            "following Table object(s) declare no schema: " + ", ".join(missing)
        )


def initialise_delta_tables(specs, *, spark, spark_root) -> InitialiseReport:
    """Create missing zero-row Delta tables under ``spark_root``.

    ``spark_root`` is the Lakehouse root that hosts ``Tables/...`` — the local
    filesystem root locally, the ``abfss://`` OneLake root on Fabric. Existing
    valid Delta tables (and their data) are left completely unchanged. A missing
    schema fails the build before any table is created.
    """

    from .load import _join_root
    from .spark_io import delta_exists, struct_type

    specs = list(specs)
    validate_delta_specs(specs)

    created: list[str] = []
    existing: list[str] = []
    for spec in specs:
        table_path = _join_root(spark_root, spec["materialisation"])
        if delta_exists(spark, table_path):
            existing.append(spec["id"])
            continue
        empty = spark.createDataFrame([], schema=struct_type(spec["schema"]))
        empty.write.format("delta").mode("overwrite").save(str(table_path))
        created.append(spec["id"])

    return InitialiseReport(
        root=str(spark_root),
        created=tuple(created),
        existing=tuple(existing),
    )
