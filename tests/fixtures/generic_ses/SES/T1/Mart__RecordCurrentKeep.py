"""
Table ID: Mart.RecordCurrentKeep
Description: Current records that keep missing keys.
Lineage: Reads the raw drop directly; never deletes missing keys.
Primary key: record_id
Incremental: true
Schema:
  record_id: string
  group_id: string
  amount: int
"""

from pathlib import Path

from weaver_runtime.dbrep.objects import Table


class Mart__RecordCurrentKeep(Table):
    def read(self, spark):
        drop = self.repo["T0.Raw.Drop"]
        csv = str(Path(drop) / "drop.csv")
        return spark.read.option("header", True).csv(csv), ()
