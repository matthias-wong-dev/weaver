"""Real SQL backend for the database-representation subsystem.

Ported and adapted from the legacy ``source`` SQL SES machinery: T-SQL DDL
inference, backing-table/view generation, and ETL load stored procedures, plus a
Fabric Warehouse connection and a layered (topological, bounded-parallel) build
executor. No product/environment defaults live here — server and database always
come from config.
"""

from __future__ import annotations
