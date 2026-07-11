"""
Table ID: Stage.Record
Description: Normalised records.
Lineage: Reads the raw drop CSV and types it.
Primary key: record_id
Schema:
  record_id: string
  group_id: string
  amount: int
"""

from pathlib import Path

from weaver_runtime.dbrep.objects import Table


class Stage__Record(Table):
    def read(self, spark):
        drop = self.repo["T0.Raw.Drop"]
        csv = str(Path(drop) / "drop.csv")
        return spark.read.option("header", True).csv(csv)
