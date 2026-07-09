"""Installed runtime: orchestrator, context, load policy, logging, rejects.

This subpackage is what the installed bundle runs at load time. It reads the
manifest and load plan, discovers object files structurally, validates them
against the manifest, and (when Spark is available) executes the load in
dependency order. PySpark is imported lazily inside the load policy only.
"""

from __future__ import annotations
