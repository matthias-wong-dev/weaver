"""Lightweight Microsoft Fabric Warehouse connector for integration testing."""

from __future__ import annotations

from dataclasses import dataclass
import os
import struct


DEFAULT_SERVER = (
    "pwr2h2pen2uuvb4rxrfbjz6nc4-yap6uh3fj2gezdnp6uajowpmi4"
    ".datawarehouse.fabric.microsoft.com"
)
DEFAULT_DATABASE = "T2"


@dataclass(frozen=True)
class QueryResult:
    columns: list[str]
    rows: list[tuple]
    rowcount: int


def build_connection_string(
    server: str | None = None,
    database: str | None = None,
) -> str:
    """Build an ODBC connection string for Fabric Warehouse."""

    server = server or os.environ.get("FABRIC_WAREHOUSE_SERVER") or DEFAULT_SERVER
    database = database or os.environ.get("FABRIC_WAREHOUSE_DATABASE") or DEFAULT_DATABASE
    parts = [
        "Driver={ODBC Driver 18 for SQL Server}",
        f"Database={database}",
        f"Server=tcp:{server},1433",
        "Encrypt=yes",
        "TrustServerCertificate=no",
    ]
    return ";".join(parts) + ";"


def connect(
    server: str | None = None,
    database: str | None = None,
    timeout: int = 30,
):
    """Open a Fabric Warehouse connection using DefaultAzureCredential."""

    try:
        import pyodbc
        from azure.identity import DefaultAzureCredential
    except ImportError as exc:
        raise RuntimeError(
            "Install pyodbc and azure-identity to use the Fabric connector."
        ) from exc

    credential = DefaultAzureCredential()
    token = credential.get_token("https://database.windows.net/.default").token
    token_bytes = token.encode("utf-16-le")
    attrs_before = {
        1256: struct.pack(f"<I{len(token_bytes)}s", len(token_bytes), token_bytes)
    }
    return pyodbc.connect(
        build_connection_string(server=server, database=database),
        attrs_before=attrs_before,
        timeout=timeout,
    )


def run_sql(
    sql: str,
    *,
    server: str | None = None,
    database: str | None = None,
    timeout: int = 30,
) -> QueryResult:
    """Execute SQL and return the first result set, if any."""

    with connect(server=server, database=database, timeout=timeout) as conn:
        cursor = conn.cursor()
        cursor.execute(sql)

        while cursor.description is None:
            if not cursor.nextset():
                conn.commit()
                return QueryResult(columns=[], rows=[], rowcount=cursor.rowcount)

        columns = [column[0] for column in cursor.description]
        rows = [tuple(row) for row in cursor.fetchall()]
        return QueryResult(columns=columns, rows=rows, rowcount=cursor.rowcount)


def describe_first_result_set(
    sql: str,
    *,
    server: str | None = None,
    database: str | None = None,
    timeout: int = 30,
) -> list[dict]:
    """Return Fabric metadata for the first result set of a SQL batch."""

    with connect(server=server, database=database, timeout=timeout) as conn:
        cursor = conn.cursor()
        cursor.execute("exec sys.sp_describe_first_result_set @tsql = ?", sql)
        columns = [column[0] for column in cursor.description]
        return [dict(zip(columns, tuple(row))) for row in cursor.fetchall()]


def generate_create_table_sql(
    sql: str,
    target_table_name: str,
    *,
    identity_column: str | None = None,
    primary_key_columns: list[str] | None = None,
    server: str | None = None,
    database: str | None = None,
    timeout: int = 30,
    type_mapping_path=None,
) -> str:
    """Describe SQL in Fabric and return backing table/view DDL."""

    from source.ddlhelper import build_create_table_sql_from_describe_rows

    rows = describe_first_result_set(
        sql,
        server=server,
        database=database,
        timeout=timeout,
    )
    return build_create_table_sql_from_describe_rows(
        rows,
        target_table_name,
        identity_column=identity_column,
        primary_key_columns=primary_key_columns,
        type_mapping_path=type_mapping_path,
    )
