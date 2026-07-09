from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"


def test_core_modules_do_not_import_pyspark() -> None:
    """Importing the full core build/load path must not pull in PySpark.

    Run in a subprocess so PySpark imported by the optional Spark tier in this
    session cannot mask a real leak.
    """

    script = textwrap.dedent(
        """
        import sys

        import weaver_runtime.cli
        import weaver_runtime.dbrep.build
        import weaver_runtime.dbrep.build.planner
        import weaver_runtime.dbrep.build.runtime_bundle
        import weaver_runtime.dbrep.build.manifest
        import weaver_runtime.dbrep.targets
        import weaver_runtime.dbrep.runtime.orchestrator
        import weaver_runtime.dbrep.ses.discovery
        import weaver_runtime.dbrep.ses.dependencies
        import weaver_runtime.dbrep.config

        leaked = [name for name in sys.modules if name == "pyspark" or name.startswith("pyspark.")]
        assert not leaked, f"pyspark leaked into core import: {leaked}"
        print("ok")
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        env={"PYTHONPATH": str(SRC)},
    )
    assert result.returncode == 0, result.stderr
    assert "ok" in result.stdout
