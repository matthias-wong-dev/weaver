"""Spark execution engine for target-only load.

Instantiates installed object classes, runs selected Folder and Table steps in
dependency order, and applies the governed load policy to Delta tables. PySpark and Delta
are imported lazily so importing this module (and the whole core) stays free of
Spark.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

from ..errors import LoadError
from ..objects import Folder, Table, View, WeaverObject
from .context import LoadContext, Repo
from .load_policy import run_table_load
from .logging import LoadReport, StepLog
from .rejects import write_rejects


def create_delta_session(
    app_name: str = "weaver-load",
    *,
    lakehouse_root: Path | None = None,
):
    """Create a local Delta-enabled Spark session."""

    from delta import configure_spark_with_delta_pip
    from pyspark.sql import SparkSession

    builder = (
        SparkSession.builder.appName(app_name)
        .master(os.environ.get("WEAVER_SPARK_MASTER", "local[1]"))
        .config("spark.ui.enabled", "false")
        .config("spark.sql.shuffle.partitions", "1")
        .config("spark.sql.extensions", "io.delta.sql.DeltaSparkSessionExtension")
        .config(
            "spark.sql.catalog.spark_catalog",
            "org.apache.spark.sql.delta.catalog.DeltaCatalog",
        )
    )
    if lakehouse_root is not None:
        ivy_dir = lakehouse_root / "_ivy"
        local_dir = lakehouse_root / "_spark_tmp"
        warehouse_dir = lakehouse_root / "_warehouse"
        ivy_dir.mkdir(parents=True, exist_ok=True)
        local_dir.mkdir(parents=True, exist_ok=True)
        warehouse_dir.mkdir(parents=True, exist_ok=True)
        builder = (
            builder
            .config("spark.jars.ivy", str(ivy_dir))
            .config("spark.local.dir", str(local_dir))
            .config("spark.sql.warehouse.dir", str(warehouse_dir))
        )
    return configure_spark_with_delta_pip(builder).getOrCreate()


def execute_load_plan(
    *,
    runtime_root,
    catalogue,
    dictionaries,
    discovered,
    steps,
    include_static: bool = False,
    spark=None,
    spark_root=None,
) -> LoadReport:
    """Execute installed load steps in order and return a report.

    ``lakehouse_root`` (derived from ``runtime_root``) is used for Python file IO
    (Folder objects) and reading the installed runtime. ``spark_root`` is where
    Spark reads/writes Delta tables and Folder outputs; it defaults to
    ``lakehouse_root`` for local filesystem lakehouses, but on Fabric it is the
    ``abfss://`` OneLake path (the FUSE mount cannot host Spark Delta writes).
    """

    runtime_root = Path(runtime_root)
    lakehouse_root = runtime_root.parents[2]
    if spark_root is None:
        spark_root = lakehouse_root
    catalogue_by_id = {entry["id"]: entry for entry in catalogue.get("objects", [])}
    schema_by_id = _schema_by_object(dictionaries.get("column", {}))

    _ensure_on_path(runtime_root)

    own_spark = False
    if any(step["kind"] == "Table" for step in steps) and spark is None:
        spark = create_delta_session(lakehouse_root=lakehouse_root)
        own_spark = True

    loaded: dict = {}
    step_logs: list[StepLog] = []
    try:
        for step in steps:
            step_logs.append(
                _run_step(
                    step,
                    discovered,
                    catalogue_by_id,
                    schema_by_id,
                    loaded,
                    lakehouse_root,
                    spark_root,
                    runtime_root,
                    spark,
                )
            )
    except LoadError:
        raise
    except Exception as exc:  # surface object/Spark failures as LoadError, stop on failure
        raise LoadError(str(exc)) from exc
    finally:
        if own_spark and spark is not None:
            spark.stop()

    return LoadReport(
        runtime_root=str(runtime_root),
        executed=True,
        ok=True,
        steps=tuple(step_logs),
        message="load complete",
    )


def _run_step(
    step,
    discovered,
    catalogue_by_id,
    schema_by_id,
    loaded,
    lakehouse_root,
    spark_root,
    runtime_root,
    spark,
) -> StepLog:
    object_id = step["object"]
    kind = step["kind"]
    source_object = discovered[object_id]
    entry = catalogue_by_id[object_id]
    materialisation = entry["materialisation"]
    log = StepLog(object_id=object_id, kind=kind, status="running")

    repo = _make_repo(catalogue_by_id, source_object.database, object_id, spark_root, spark)

    if kind == "Folder":
        folder = lakehouse_root / materialisation
        folder.mkdir(parents=True, exist_ok=True)
        context = LoadContext(
            runtime_root=runtime_root,
            lakehouse_root=lakehouse_root,
            object_id=object_id,
            kind=kind,
            materialisation=materialisation,
            repo=repo,
            object_path=folder,
            spark=spark,
            metadata=source_object.metadata,
        )
        _instantiate(source_object, context).load()
        if not folder.is_dir():
            raise LoadError(f"folder object {object_id} did not create {folder}")
        loaded[object_id] = folder
        log.status = "ok"
        return log

    if kind == "Table":
        table_path = _join_root(spark_root, materialisation)
        context = LoadContext(
            runtime_root=runtime_root,
            lakehouse_root=lakehouse_root,
            object_id=object_id,
            kind=kind,
            materialisation=materialisation,
            repo=repo,
            object_path=table_path,
            spark=spark,
            metadata=source_object.metadata,
        )
        frame = _instantiate(source_object, context).read(spark)
        schema = schema_by_id.get(object_id, source_object.metadata.schema)
        frame = _align_frame_to_schema(frame, schema)
        incoming = [row.asDict(recursive=True) for row in frame.collect()]
        existing = _read_delta_rows(spark, table_path)
        metadata = source_object.metadata
        outcome = run_table_load(
            existing,
            incoming,
            primary_key=metadata.primary_key,
            schema=schema,
            auto_delete=metadata.auto_delete,
            load_mode=metadata.load_mode,
        )
        _write_delta(spark, outcome, table_path, schema)
        write_rejects(lakehouse_root, object_id, outcome.rejected)
        loaded[object_id] = spark.read.format("delta").load(str(table_path))

        counts = outcome.counts()
        log.input = counts["input"]
        log.accepted = counts["accepted"]
        log.rejected = counts["rejected"]
        log.inserted = counts["inserted"]
        log.updated = counts["updated"]
        log.deleted = counts["deleted"]
        log.auto_delete_ran = outcome.auto_delete_ran
        log.status = "ok"
        return log

    log.status = "skipped"
    log.message = f"unsupported kind for lakehouse load: {kind}"
    return log


def _make_repo(catalogue_by_id: dict, database: str, current_id: str, spark_root, spark) -> Repo:
    def resolve(key: str):
        parts = key.split(".")
        object_id = key if len(parts) >= 3 else f"{database}.{key}"
        entry = catalogue_by_id.get(object_id)
        if entry is None:
            raise LoadError(
                f"dependency {key!r} ({object_id}) required by {current_id} "
                "is not in the installed catalogue"
            )
        path = _join_root(spark_root, entry["materialisation"])
        kind = entry.get("kind")
        if kind == "Folder":
            return path
        if kind == "Table":
            if spark is None:
                raise LoadError(f"table dependency {object_id} requires Spark")
            return spark.read.format("delta").load(str(path))
        if kind == "View":
            return entry
        raise LoadError(f"dependency {object_id} has unsupported kind {kind!r}")

    return Repo(resolve)


def _instantiate(source_object, context: LoadContext):
    module = _import_object_module(source_object)
    cls = _find_object_class(module)
    return cls(context)


def _import_object_module(source_object):
    module_name = "weaver_obj_" + source_object.id.replace(".", "_")
    spec = importlib.util.spec_from_file_location(module_name, source_object.source_path)
    if spec is None or spec.loader is None:
        raise LoadError(f"cannot import object module: {source_object.source_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _find_object_class(module):
    candidates = [
        value
        for value in vars(module).values()
        if isinstance(value, type)
        and issubclass(value, WeaverObject)
        and value.__module__ == module.__name__
        and value not in (WeaverObject, Folder, Table, View)
    ]
    if len(candidates) != 1:
        raise LoadError(
            f"object file must define exactly one Weaver object, found {len(candidates)}"
        )
    return candidates[0]


def _join_root(root, relative: str):
    """Join a lakehouse root and a materialisation.

    Uses string joins for ``abfss://`` (and other URL) roots and path joins for
    local filesystem roots.
    """

    root_str = str(root)
    if "://" in root_str:
        return f"{root_str.rstrip('/')}/{relative}"
    return Path(root) / relative


def _delta_exists(spark, table_path) -> bool:
    path_str = str(table_path)
    if "://" in path_str:
        try:
            spark.read.format("delta").load(path_str).schema
            return True
        except Exception:
            return False
    return (Path(table_path) / "_delta_log").is_dir()


def _read_delta_rows(spark, table_path) -> list[dict]:
    if not _delta_exists(spark, table_path):
        return []
    frame = spark.read.format("delta").load(str(table_path))
    return [row.asDict(recursive=True) for row in frame.collect()]


def _align_frame_to_schema(frame, schema):
    if not schema:
        return frame

    from pyspark.sql import functions as F

    declared_columns = [column for column, _ in schema]
    existing_columns = set(frame.columns)
    missing = [column for column in declared_columns if column not in existing_columns]
    if missing:
        raise LoadError("missing declared schema column(s): " + ", ".join(missing))

    try:
        return frame.select(
            *[
                F.col(column).cast(type_name).alias(column)
                for column, type_name in schema
            ]
        )
    except Exception as exc:
        raise LoadError("failed to apply Spark SQL schema casts") from exc


def _write_delta(spark, outcome, table_path, schema) -> None:
    rows = outcome.final_rows
    if rows:
        frame = spark.createDataFrame(rows, schema=_struct_type(schema))
    elif schema:
        frame = spark.createDataFrame([], schema=_struct_type(schema))
    elif _delta_exists(spark, table_path):
        empty = spark.read.format("delta").load(str(table_path)).schema
        frame = spark.createDataFrame([], schema=empty)
    else:
        return
    frame.write.format("delta").mode("overwrite").option(
        "overwriteSchema", "true"
    ).save(str(table_path))


def _struct_type(schema):
    if not schema:
        return None
    from pyspark.sql.types import StructField, StructType

    fields = [
        StructField(column, _spark_data_type(type_name), True)
        for column, type_name in schema
    ]
    return StructType(fields)


def _spark_data_type(type_name: str):
    """Parse a Spark SQL / Delta column type string into a PySpark DataType."""

    from pyspark.sql.types import _parse_datatype_string

    try:
        return _parse_datatype_string(type_name)
    except Exception as exc:
        raise LoadError(
            f"failed to parse Spark SQL schema type {type_name!r}; "
            "use a Spark/Delta-compatible type string"
        ) from exc


def _schema_by_object(column_dictionary: dict) -> dict[str, tuple[tuple[str, str], ...]]:
    grouped: dict[str, list[tuple[int, str, str]]] = {}
    for entry in column_dictionary.get("columns", []):
        object_id = entry.get("object_id")
        column = entry.get("column")
        type_name = entry.get("type")
        if not object_id or not column or not type_name:
            continue
        grouped.setdefault(object_id, []).append(
            (int(entry.get("ordinal", 0)), column, type_name)
        )
    return {
        object_id: tuple((column, type_name) for _, column, type_name in sorted(columns))
        for object_id, columns in grouped.items()
    }


def _ensure_on_path(runtime_root: Path) -> None:
    for candidate in (runtime_root / "objects", runtime_root):
        path = str(candidate)
        if path not in sys.path:
            sys.path.insert(0, path)
