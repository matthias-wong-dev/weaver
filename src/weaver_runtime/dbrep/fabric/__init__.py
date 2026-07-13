"""Fabric Lakehouse backend for dbrep Files/Delta targets.

Build stages the runtime bundle locally and uploads ``Files/`` to the target
Lakehouse via OneLake. Load submits the installed orchestrator to Fabric Spark
through the Livy API. Both reuse the existing operational plumbing under
``scripts/`` (repository sync and Spark session helpers) via the
legacy loader, so this backend runs from the weaver repo (not inside the bundle).
"""

from __future__ import annotations
