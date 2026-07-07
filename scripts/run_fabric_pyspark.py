#!/usr/bin/env python3
"""Run PySpark code in a Microsoft Fabric Spark Livy session."""

from __future__ import annotations

import sys
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import sparksession  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(sparksession.main())
