"""
Table ID: Mart.RecordAudit
Description: Append-only audit of every raw row.
Lineage: Reads the raw drop directly and appends all rows.
Primary key: audit_id
Incremental: true
Schema:
  audit_id: string
  record_id: string
  group_id: string
  amount: int
"""

from pathlib import Path

from weaver_runtime.dbrep.objects import Table


class Mart__RecordAudit(Table):
    def read(self, spark):
        from pyspark.sql import functions as F

        drop = self.repo["T0.Raw.Drop"]
        csv = str(Path(drop) / "drop.csv")
        frame = spark.read.option("header", True).csv(csv)
        return frame.withColumn("audit_id", F.expr("uuid()")), ()
