"""
Folder ID: Raw.Drop
Description: Raw record drop folder.
Lineage: Writes the current run's raw CSV into the landing folder.
"""

import os
import shutil
from pathlib import Path

from weaver_runtime.dbrep.objects import Folder


class RawDrop(Folder):
    def load(self):
        target = Path(self.context.object_path)
        target.mkdir(parents=True, exist_ok=True)
        destination = target / "drop.csv"
        source = os.environ.get("WEAVER_TEST_RUN_CSV")
        if source:
            shutil.copyfile(source, destination)
        elif not destination.exists():
            destination.write_text("record_id,group_id,amount\n", encoding="utf-8")
