"""Execute SQL against a Fabric Warehouse / SQL endpoint via pyodbc + AAD."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .auth import sql_token_attrs
from .settings import DEFAULT_SQL_SCOPE


class SqlError(RuntimeError):
    """Raised when a SQL execution cannot be performed."""


def build_connection_string(server: str, database: str | None) -> str:
    parts = [
        "Driver={ODBC Driver 18 for SQL Server}",
        f"Server=tcp:{server},1433",
        "Encrypt=yes",
        "TrustServerCertificate=no",
    ]
    if database:
        parts.insert(1, f"Database={database}")
    return ";".join(parts) + ";"


def connect(server: str | None, database: str | None = None, *, sql_scope: str = DEFAULT_SQL_SCOPE):
    """Open a pyodbc connection to a Fabric/Azure SQL endpoint using an AAD token."""

    if not server:
        raise SqlError("SQL server is required")
    import pyodbc

    return pyodbc.connect(
        build_connection_string(server, database),
        attrs_before=sql_token_attrs(sql_scope),
        timeout=30,
    )


@dataclass(frozen=True)
class SqlResult:
    columns: list[str]
    rows: list[tuple[Any, ...]]
    rowcount: int


def execute(
    sql: str,
    server: str | None,
    database: str | None = None,
    *,
    sql_scope: str = DEFAULT_SQL_SCOPE,
) -> SqlResult:
    """Execute one SQL batch and return columns, rows, and affected rowcount."""

    with connect(server, database, sql_scope=sql_scope) as conn:
        cursor = conn.cursor()
        cursor.execute(sql)
        if cursor.description is None:
            conn.commit()
            return SqlResult(columns=[], rows=[], rowcount=cursor.rowcount)
        columns = [column[0] for column in cursor.description]
        rows = [tuple(row) for row in cursor.fetchall()]
        return SqlResult(columns=columns, rows=rows, rowcount=cursor.rowcount)
