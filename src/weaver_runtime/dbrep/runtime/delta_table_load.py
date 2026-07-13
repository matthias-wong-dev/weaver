"""Spark-native physical execution for governed Delta table loads.

The pure :mod:`load_policy` module remains the semantic reference model. This
module applies the same policy with DataFrame validation and Delta operations,
without collecting either the accepted source or the target table into Python.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import reduce
from operator import or_
from typing import Sequence

from ..ses.metadata import APPEND, REPLACE
from .load_policy import (
    REASON_BLANK_PK,
    REASON_DUPLICATE_PK,
    prepare_table_load,
)
from .spark_io import delta_exists


@dataclass(frozen=True)
class DeltaTableLoadOutcome:
    """Counts and reject rows produced by one physical Delta write."""

    input_count: int
    accepted_count: int
    rejected_count: int
    inserted: int
    updated: int
    deleted: int
    rejected: tuple[dict, ...]
    reconciliation_ran: bool
    explicit_delete_keys_read: int = 0
    explicit_delete_keys_matched: int = 0
    explicit_delete_keys_unmatched: int = 0
    wrote: bool = False

    def counts(self) -> dict[str, int]:
        return {
            "input": self.input_count,
            "accepted": self.accepted_count,
            "rejected": self.rejected_count,
            "inserted": self.inserted,
            "updated": self.updated,
            "deleted": self.deleted,
        }


def execute_delta_table_load(
    spark,
    frame,
    table_path,
    *,
    primary_key: Sequence[str] = (),
    schema: Sequence[tuple[str, str]] = (),
    is_incremental: bool = False,
    load_mode: str | None = None,
    explicit_delete_keys: Sequence[Sequence] = (),
    object_name: str = "",
) -> DeltaTableLoadOutcome:
    """Validate ``frame`` and apply its governed write directly in Delta."""

    plan = prepare_table_load(
        primary_key=primary_key,
        schema=schema,
        is_incremental=is_incremental,
        load_mode=load_mode,
        explicit_delete_keys=explicit_delete_keys,
        object_name=object_name,
    )
    _require_primary_key_columns(frame, plan.primary_key)

    validated, accepted, input_count, accepted_count, rejected = _validate_frame(
        frame, plan.primary_key
    )
    try:
        table_preexisted = delta_exists(spark, table_path)
        if not table_preexisted:
            _initialise_table(accepted, table_path)

        # An empty incremental upsert (the normal idempotent case) and an empty
        # append have no physical work. In particular, do not create a Delta
        # commit or inspect the existing target rows.
        if accepted_count == 0 and not plan.explicit_delete_keys and (
            plan.mode == APPEND or (is_incremental and plan.primary_key)
        ):
            return _outcome(
                input_count,
                accepted_count,
                rejected,
                plan.reconciliation_ran,
                wrote=not table_preexisted,
            )

        if plan.mode == APPEND:
            accepted.write.format("delta").mode("append").save(str(table_path))
            return _outcome(
                input_count,
                accepted_count,
                rejected,
                plan.reconciliation_ran,
                inserted=accepted_count,
                wrote=True,
            )

        if plan.mode == REPLACE or not plan.primary_key:
            existing_count = _delta_table(spark, table_path).toDF().count()
            accepted.write.format("delta").mode("overwrite").option(
                "overwriteSchema", "true"
            ).save(str(table_path))
            return _outcome(
                input_count,
                accepted_count,
                rejected,
                plan.reconciliation_ran,
                inserted=accepted_count,
                deleted=existing_count,
                wrote=True,
            )

        return _merge_upsert(
            spark,
            accepted,
            table_path,
            plan.primary_key,
            tuple(column for column, _ in schema) or tuple(accepted.columns),
            plan.explicit_delete_keys,
            input_count,
            accepted_count,
            rejected,
            plan.reconciliation_ran,
        )
    finally:
        validated.unpersist()


def _validate_frame(frame, primary_key):
    """Return cached validation state, accepted frame, counts, and rejects."""

    from pyspark.sql import Window, functions as F

    original_columns = tuple(frame.columns)
    if not primary_key:
        validated = frame.persist()
        input_count = validated.count()
        return validated, validated, input_count, input_count, ()

    reason_column = _temporary_column(original_columns, "_weaver_reject_reason")
    rank_column = _temporary_column(
        (*original_columns, reason_column), "_weaver_primary_key_rank"
    )
    blank_key = reduce(
        or_,
        (
            F.col(column).isNull()
            | (F.trim(F.col(column).cast("string")) == F.lit(""))
            for column in primary_key
        ),
    )
    ranked = frame.withColumn(
        rank_column,
        F.row_number().over(Window.partitionBy(*primary_key).orderBy(F.lit(1))),
    )
    validated = ranked.withColumn(
        reason_column,
        F.when(blank_key, F.lit(REASON_BLANK_PK))
        .when(F.col(rank_column) > 1, F.lit(REASON_DUPLICATE_PK))
        .otherwise(F.lit(None).cast("string")),
    ).persist()

    grouped = validated.groupBy(reason_column).count().collect()
    counts = {row[reason_column]: int(row["count"]) for row in grouped}
    accepted_count = counts.get(None, 0)
    input_count = sum(counts.values())
    accepted = validated.where(F.col(reason_column).isNull()).select(*original_columns)

    rejected_rows = []
    for row in (
        validated.where(F.col(reason_column).isNotNull())
        .select(*original_columns, reason_column)
        .collect()
    ):
        item = row.asDict(recursive=True)
        item["_reject_reason"] = item.pop(reason_column)
        rejected_rows.append(item)
    return validated, accepted, input_count, accepted_count, tuple(rejected_rows)


def _merge_upsert(
    spark,
    accepted,
    table_path,
    primary_key,
    columns,
    explicit_delete_keys,
    input_count,
    accepted_count,
    rejected,
    reconciliation_ran,
):
    from pyspark.sql import functions as F

    delta = _delta_table(spark, table_path)
    operation_column = _temporary_column(columns, "_weaver_operation")
    source = accepted.withColumn(operation_column, F.lit("upsert"))

    explicit_read = len(explicit_delete_keys)
    explicit_matched = 0
    if explicit_delete_keys:
        delete_frame = _delete_key_frame(
            spark, accepted, primary_key, explicit_delete_keys
        )
        # An upsert wins when the same key was also explicitly deleted.
        delete_frame = delete_frame.join(
            accepted.select(*primary_key).distinct(), list(primary_key), "left_anti"
        )
        explicit_matched = (
            delta.toDF()
            .select(*primary_key)
            .join(delete_frame, list(primary_key), "inner")
            .select(*primary_key)
            .distinct()
            .count()
        )
        delete_source = delete_frame
        for column in columns:
            if column not in primary_key:
                delete_source = delete_source.withColumn(
                    column, F.lit(None).cast(accepted.schema[column].dataType)
                )
        delete_source = delete_source.select(*columns).withColumn(
            operation_column, F.lit("delete")
        )
        source = source.unionByName(delete_source)

    condition = " and ".join(
        f"target.{_quote(column)} = source.{_quote(column)}"
        for column in primary_key
    )
    upsert_condition = f"source.{_quote(operation_column)} = 'upsert'"
    delete_condition = f"source.{_quote(operation_column)} = 'delete'"
    values = {column: f"source.{_quote(column)}" for column in columns}

    merger = delta.alias("target").merge(source.alias("source"), condition)
    if explicit_delete_keys:
        merger = merger.whenMatchedDelete(condition=delete_condition)
    merger = merger.whenMatchedUpdate(condition=upsert_condition, set=values)
    merger = merger.whenNotMatchedInsert(condition=upsert_condition, values=values)
    if reconciliation_ran:
        merger = merger.whenNotMatchedBySourceDelete()
    merger.execute()

    metrics = _last_operation_metrics(delta)
    inserted = _metric(metrics, "numTargetRowsInserted", "numInsertedRows")
    updated = _metric(metrics, "numTargetRowsUpdated", "numUpdatedRows")
    deleted = _metric(metrics, "numTargetRowsDeleted", "numDeletedRows")
    return _outcome(
        input_count,
        accepted_count,
        rejected,
        reconciliation_ran,
        inserted=inserted,
        updated=updated,
        deleted=deleted,
        explicit_read=explicit_read,
        explicit_matched=explicit_matched,
        wrote=True,
    )


def _delete_key_frame(spark, accepted, primary_key, explicit_delete_keys):
    from pyspark.sql import functions as F

    raw = spark.createDataFrame(
        [dict(zip(primary_key, key, strict=True)) for key in explicit_delete_keys]
    )
    return raw.select(
        *[
            F.col(column).cast(accepted.schema[column].dataType).alias(column)
            for column in primary_key
        ]
    ).distinct()


def _outcome(
    input_count,
    accepted_count,
    rejected,
    reconciliation_ran,
    *,
    inserted=0,
    updated=0,
    deleted=0,
    explicit_read=0,
    explicit_matched=0,
    wrote=False,
):
    return DeltaTableLoadOutcome(
        input_count=input_count,
        accepted_count=accepted_count,
        rejected_count=len(rejected),
        inserted=inserted,
        updated=updated,
        deleted=deleted,
        rejected=rejected,
        reconciliation_ran=reconciliation_ran,
        explicit_delete_keys_read=explicit_read,
        explicit_delete_keys_matched=explicit_matched,
        explicit_delete_keys_unmatched=explicit_read - explicit_matched,
        wrote=wrote,
    )


def _require_primary_key_columns(frame, primary_key) -> None:
    missing = [column for column in primary_key if column not in frame.columns]
    if missing:
        from ..errors import LoadError

        raise LoadError(
            "primary key columns are not part of the staged DataFrame: "
            + ", ".join(missing)
        )


def _initialise_table(frame, table_path) -> None:
    frame.limit(0).write.format("delta").mode("overwrite").save(str(table_path))


def _delta_table(spark, table_path):
    from delta.tables import DeltaTable

    return DeltaTable.forPath(spark, str(table_path))


def _last_operation_metrics(delta) -> dict[str, str]:
    row = delta.history(1).select("operationMetrics").head()
    return dict(row["operationMetrics"] or {})


def _metric(metrics, *names) -> int:
    for name in names:
        if name in metrics:
            return int(metrics[name])
    return 0


def _quote(identifier: str) -> str:
    return "`" + identifier.replace("`", "``") + "`"


def _temporary_column(columns, preferred: str) -> str:
    name = preferred
    while name in columns:
        name += "_"
    return name
