"""
Folder ID: Sales.OrderCsv
Description: Raw order extracts as landed CSV, one file per monthly extract.
Lineage: Lands each monthly order extract into the landing folder; extracts already present are left in place.
File key: "**/*.csv"
Incremental: true
"""

from pathlib import Path

from ._helpers.sample_source import ORDER_SNAPSHOTS
from weaver_runtime.dbrep.objects import Folder


class Sales__OrderCsv(Folder):
    def read(self):
        # Watermark: the extract filenames already landed. Reading the
        # destination is allowed; writing to it directly is not.
        destination = Path(str(self.path))
        already_landed = {path.name for path in destination.glob("*.csv")}

        # Stage only extracts we have not landed before. With Incremental: true,
        # extracts already in the target are retained, so re-running is a no-op.
        with self.staging_folder() as staging:
            for file_name, text in ORDER_SNAPSHOTS.items():
                if file_name not in already_landed:
                    (Path(staging.path) / file_name).write_text(text, encoding="utf-8")

        return staging, ()
