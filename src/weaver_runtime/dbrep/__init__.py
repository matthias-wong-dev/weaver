"""Generic database-representation build and load subsystem for Weaver.

Weaver builds between named *database representations*. A representation is a
typed third-level name (``SES``/``Files``/``Delta``/``SQL``) living on a
host/server declared in environment config. Build installs physical structure,
a runtime bundle, a manifest, and a load plan; load is target-only and runs the
installed dependency graph.

This subsystem is deliberately domain-neutral: it contains no product names,
endpoints, or paths. All concrete names live in caller config and test
fixtures.
"""

from __future__ import annotations
