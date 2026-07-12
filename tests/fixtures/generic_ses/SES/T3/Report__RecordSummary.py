"""
Table ID: Report.RecordSummary
Description: Per-group summary report.
Lineage: Reads the cross-database aggregate.
Primary key: group_id
Schema:
  group_id: string
  amount: long
"""

from weaver_runtime.dbrep.objects import Table


class Report__RecordSummary(Table):
    def read(self, spark):
        aggregate = self.repo["T2.Mart.RecordAggregate"]
        return aggregate.select("group_id", "amount"), ()
