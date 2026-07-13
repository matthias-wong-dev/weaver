"""
Table ID: Sales.Order
Description: One row per order, typed from the landed order extracts.
Lineage: Reads the order CSV extracts landed in Raw.Sales.OrderCsv and types them; past orders are retained.
Primary key: order_id
Incremental: true
Schema:
  order_id: string
  customer_id: string
  order_date: date
  amount: decimal(12,2)
  loaded_at: timestamp
"""

from ._helpers.csv_frames import read_typed_csv
from weaver_runtime.dbrep.objects import Table


class Sales__Order(Table):
    def read(self):
        from pyspark.sql import functions as F

        source = self.repo["Raw.Sales.OrderCsv"]
        typed = read_typed_csv(self.spark, source, self.schema)

        # Incremental: true with a primary key — orders absent from this run are
        # kept, so the table accumulates history as new monthly extracts arrive.
        return typed.withColumn("loaded_at", F.current_timestamp()), ()
