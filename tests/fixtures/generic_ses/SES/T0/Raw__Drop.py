"""
Folder ID: Raw.Drop
Description: Raw record drop folder.
Lineage: Writes the current run's raw CSV into the landing folder.
"""

import os
import shutil

from weaver_runtime.dbrep.objects import Folder


class Raw__Drop(Folder):
    def read(self):
        with self.staging_folder() as staging:
            destination = staging.path / "drop.csv"
            source = os.environ.get("WEAVER_TEST_RUN_CSV")

            if source:
                shutil.copyfile(source, destination)
            else:
                destination.write_text(
                    "record_id,group_id,amount\n",
                    encoding="utf-8",
                )

        return staging, ()
