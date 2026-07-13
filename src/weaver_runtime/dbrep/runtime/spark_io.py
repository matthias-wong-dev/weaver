"""Shared Spark/Delta IO and frame helpers for the load runtime.

Both the executor (``runtime/load.py``) and the object authoring accessors
(``LoadContext.current_dataframe`` / ``empty_frame``) use these, so there is one
Fabric-correct implementation of "does this Delta table exist" and "read it".

PySpark and Delta are imported lazily inside each function, so importing this
module keeps the core free of Spark (the ``objects.py`` authoring surface reaches
these only through ``LoadContext`` methods, never at import time).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..errors import LoadError


def delta_exists(spark, table_path) -> bool:
    """True if a Delta table is materialised at ``table_path``.

    Handles both local filesystem paths (``_delta_log/`` probe) and cloud URL
    roots such as ``abfss://`` OneLake paths, where a filesystem probe is
    impossible so we attempt a metadata read instead.
    """

    path_str = str(table_path)
    if "://" in path_str:
        try:
            spark.read.format("delta").load(path_str).schema
            return True
        except Exception:
            return False
    return (Path(table_path) / "_delta_log").is_dir()


def read_delta_frame(spark, table_path):
    """Return the Delta table at ``table_path`` as a DataFrame, or ``None``.

    ``None`` means the table has never been written. Fabric-correct: existence is
    checked via :func:`delta_exists` (which understands ``abfss://``).
    """

    if spark is None or table_path is None:
        return None
    if not delta_exists(spark, table_path):
        return None
    return spark.read.format("delta").load(str(table_path))


def read_delta_rows(spark, table_path) -> list[dict]:
    """Collect the current Delta rows as plain dicts (empty when absent)."""

    frame = read_delta_frame(spark, table_path)
    if frame is None:
        return []
    return [row.asDict(recursive=True) for row in frame.collect()]


def spark_data_type(type_name: str):
    """Parse a Spark SQL / Delta column type string into a PySpark DataType."""

    from pyspark.sql.types import _parse_datatype_string

    try:
        return _parse_datatype_string(type_name)
    except Exception as exc:
        raise LoadError(
            f"failed to parse Spark SQL schema type {type_name!r}; "
            "use a Spark/Delta-compatible type string"
        ) from exc


def struct_type(schema):
    """Build a ``StructType`` from an ordered ``((column, type), ...)`` schema."""

    if not schema:
        return None
    from pyspark.sql.types import StructField, StructType

    fields = [
        StructField(column, spark_data_type(type_name), True)
        for column, type_name in schema
    ]
    return StructType(fields)


def empty_frame(spark, schema) -> Any:
    """Create an empty DataFrame matching an ordered declared schema."""

    if spark is None:
        raise LoadError("empty_frame requires an active Spark session")
    if not schema:
        raise LoadError("empty_frame requires a declared Schema")
    return spark.createDataFrame([], struct_type(schema))
