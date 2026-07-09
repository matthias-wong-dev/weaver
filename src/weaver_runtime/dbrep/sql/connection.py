"""Fabric Warehouse / SQL Server connection for the dbrep SQL backend.

Server and database always come from config — there are no product or
environment defaults here. Authentication uses an Entra ID access token
(``DefaultAzureCredential``), which picks up ``az login``. pyodbc and
azure-identity are imported lazily so importing this module stays cheap and
keeps the core dependency-light.
"""

from __future__ import annotations

import struct

from ..errors import WeaverError

_SQL_COPT_SS_ACCESS_TOKEN = 1256
_TOKEN_SCOPE = "https://database.windows.net/.default"
_DEFAULT_DRIVER = "ODBC Driver 18 for SQL Server"


class SqlConnectionError(WeaverError):
    """Raised when a SQL connection cannot be opened."""


def build_connection_string(server: str, database: str, *, driver: str = _DEFAULT_DRIVER) -> str:
    return (
        ";".join(
            [
                f"Driver={{{driver}}}",
                f"Server=tcp:{server},1433",
                f"Database={database}",
                "Encrypt=yes",
                "TrustServerCertificate=no",
            ]
        )
        + ";"
    )


def _access_token_attrs():
    try:
        from azure.identity import DefaultAzureCredential
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise SqlConnectionError("azure-identity is required for SQL targets") from exc
    token = DefaultAzureCredential().get_token(_TOKEN_SCOPE).token
    token_bytes = token.encode("utf-16-le")
    packed = struct.pack(f"<I{len(token_bytes)}s", len(token_bytes), token_bytes)
    return {_SQL_COPT_SS_ACCESS_TOKEN: packed}


def connect(server: str, database: str, *, timeout: int = 60):
    """Open a pyodbc connection to a Fabric Warehouse / SQL database."""

    try:
        import pyodbc
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise SqlConnectionError("pyodbc is required for SQL targets") from exc
    try:
        return pyodbc.connect(
            build_connection_string(server, database),
            attrs_before=_access_token_attrs(),
            timeout=timeout,
        )
    except SqlConnectionError:
        raise
    except Exception as exc:
        raise SqlConnectionError(f"failed to connect to {server}/{database}: {exc}") from exc


def execute_script(conn, sql: str) -> None:
    """Execute a (possibly multi-statement) T-SQL script and commit."""

    cursor = conn.cursor()
    cursor.execute(sql)
    _drain(cursor)
    conn.commit()


def query(conn, sql: str, params: tuple | None = None) -> list[dict]:
    """Run a query and return rows as dicts (empty list if no result set)."""

    cursor = conn.cursor()
    if params:
        cursor.execute(sql, params)
    else:
        cursor.execute(sql)
    if cursor.description is None:
        return []
    columns = [column[0] for column in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def _drain(cursor) -> None:
    while True:
        if cursor.description is not None:
            try:
                cursor.fetchall()
            except Exception:
                pass
        if not cursor.nextset():
            return
