"""
Folder ID: Sales.CustomerCsv
Description: Raw customer master snapshot as landed CSV, one file per extract.
Lineage: Writes the current customer extract into the landing folder as customers.csv.
File key: "**/*.csv"
Incremental: false
"""

from pathlib import Path

from ._helpers.sample_source import CUSTOMER_SNAPSHOT
from weaver_runtime.dbrep.objects import Folder


class Sales__CustomerCsv(Folder):
    def read(self):
        # Stage the current snapshot. With Incremental: false, whatever is staged
        # is the complete managed population — a customer file that disappears
        # from the source is removed from the target on the next load.
        with self.staging_folder() as staging:
            for file_name, text in CUSTOMER_SNAPSHOT.items():
                (Path(staging.path) / file_name).write_text(text, encoding="utf-8")

        return staging, ()
