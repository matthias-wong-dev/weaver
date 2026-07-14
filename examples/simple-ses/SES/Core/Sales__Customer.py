"""
Table ID: Sales.Customer
Description: One row per customer, typed from the landed customer snapshot.
Lineage: Reads the customer CSV landed in Raw.Sales.CustomerCsv and types it; the current file is the whole table.
Primary key: customer_id
Schema:
  customer_id: string
  customer_name: string
  segment: string
  signup_date: date
  loaded_at: timestamp
"""

from ._helpers.csv_frames import read_typed_csv
from weaver_runtime.dbrep.objects import Table


class Sales__Customer(Table):
    def read(self):
        from pyspark.sql import functions as F

        source = self.repo["Raw.Sales.CustomerCsv"]
        typed = read_typed_csv(self.spark, source, self.schema)

        # Default Incremental: false with a primary key — the rows returned here
        # become the complete table, so a customer dropped from the source file
        # is reconciled out of the target.
        return typed.withColumn("loaded_at", F.current_timestamp()), ()
