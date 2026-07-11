"""
Table ID: Mart.RecordAggregate
Description: Amount aggregated per group.
Lineage: Aggregates the typed stage records by group.
Primary key: group_id
Schema:
  group_id: string
  amount: long
"""

from weaver_runtime.dbrep.objects import Table


class Mart__RecordAggregate(Table):
    def read(self, spark):
        from pyspark.sql import functions as F

        stage = self.repo["T1.Stage.Record"]
        return stage.groupBy("group_id").agg(F.sum("amount").alias("amount"))
