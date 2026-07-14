"""CSV-to-DataFrame helpers shared by the Core typed tables.

These turn the raw CSV text landed by the Raw folders into a typed Spark
DataFrame that matches an object's declared schema. The object stays a short
statement of intent; the mechanical typing lives here.
"""

from __future__ import annotations

from pathlib import Path


def _ddl(schema) -> str:
    return ", ".join(f"`{name}` {type_name}" for name, type_name in schema)


def read_typed_csv(spark, source_dir, schema):
    """Read every CSV under ``source_dir`` into a DataFrame typed to ``schema``.

    ``schema`` is the object's declared ordered ``((name, type), ...)`` mapping —
    pass ``self.schema``. Declared columns present in the CSV are cast to their
    declared type; declared columns absent from the CSV (for example a
    ``loaded_at`` metadata column) are added as nulls for the caller to fill.
    Columns are returned in declared order.
    """

    from pyspark.sql import functions as F

    files = sorted(str(path) for path in Path(str(source_dir)).glob("*.csv"))
    if not files:
        return spark.createDataFrame([], _ddl(schema))

    frame = spark.read.option("header", True).csv(files)
    for name, type_name in schema:
        if name in frame.columns:
            frame = frame.withColumn(name, F.col(name).cast(type_name))
        else:
            frame = frame.withColumn(name, F.lit(None).cast(type_name))
    return frame.select(*[name for name, _ in schema])
