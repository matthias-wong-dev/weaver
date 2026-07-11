"""
Table ID: Mart.RecordCurrentAuto
Description: Current records with auto-delete of missing keys.
Lineage: Reads the raw drop directly so rejects gate auto-delete.
Primary key: record_id
Auto delete: true
Schema:
  record_id: string
  group_id: string
  amount: int
"""

from pathlib import Path

from weaver_runtime.dbrep.objects import Table


class Mart__RecordCurrentAuto(Table):
    def read(self, spark):
        drop = self.repo["T0.Raw.Drop"]
        csv = str(Path(drop) / "drop.csv")
        return spark.read.option("header", True).csv(csv)
