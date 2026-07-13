"""
Table ID: Mart.RecordSnapshot
Description: Full snapshot of the latest raw batch.
Lineage: Reads the raw drop directly and replaces the prior snapshot.
Schema:
  record_id: string
  group_id: string
  amount: int
"""

from pathlib import Path

from weaver_runtime.dbrep.objects import Table


class Mart__RecordSnapshot(Table):
    def read(self, spark):
        drop = self.repo["T0.Raw.Drop"]
        csv = str(Path(drop) / "drop.csv")
        return spark.read.option("header", True).csv(csv), ()
