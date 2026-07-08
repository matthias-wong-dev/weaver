#!/usr/bin/env python3
"""Run SQL against Microsoft Fabric/Azure SQL with pyodbc.

For ad hoc SQL containing shell-sensitive characters, prefer --stdin or
--file. Column names such as [Budgeted expense $b] can be changed by the shell
before this script sees them when passed through --sql with double quotes.
"""

from __future__ import annotations

import argparse
import os
import struct
import sys

import pyodbc
from azure.identity import DefaultAzureCredential


DEFAULT_SERVER = None
DEFAULT_DATABASE = None
DEFAULT_SQL = "SELECT 1 AS ok"


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


def build_access_token_attr() -> dict[int, bytes]:
    credential = DefaultAzureCredential()
    token = credential.get_token("https://database.windows.net/.default").token
    token_bytes = token.encode("utf-16-le")
    return {1256: struct.pack(f"<I{len(token_bytes)}s", len(token_bytes), token_bytes)}


def connect(server: str | None = DEFAULT_SERVER, database: str | None = DEFAULT_DATABASE) -> pyodbc.Connection:
    if not server:
        raise ValueError("SQL server is required")
    connection_string = build_connection_string(server, database)
    attrs_before = build_access_token_attr()
    return pyodbc.connect(connection_string, attrs_before=attrs_before, timeout=30)


def run_sql(
    sql: str,
    server: str | None = DEFAULT_SERVER,
    database: str | None = DEFAULT_DATABASE,
) -> tuple[list[str], list[tuple], int]:
    with connect(server=server, database=database) as conn:
        cursor = conn.cursor()
        cursor.execute(sql)
        if cursor.description is None:
            conn.commit()
            return [], [], cursor.rowcount

        columns = [column[0] for column in cursor.description]
        rows = [tuple(row) for row in cursor.fetchall()]
        return columns, rows, cursor.rowcount


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", default=os.environ.get("SQLSERVER_HOST") or DEFAULT_SERVER)
    parser.add_argument("--database", default=os.environ.get("SQLSERVER_DATABASE") or DEFAULT_DATABASE)
    parser.add_argument("--sql", default=DEFAULT_SQL)
    parser.add_argument("--file", help="Read SQL from a file instead of --sql. Use '-' for stdin.")
    parser.add_argument("--stdin", action="store_true", help="Read SQL from standard input instead of --sql.")
    parser.add_argument("--show-connection-string", action="store_true")
    args = parser.parse_args()

    if args.stdin and args.file:
        parser.error("--stdin and --file are mutually exclusive")

    return args


def read_sql_from_args(args: argparse.Namespace) -> str:
    if args.stdin or args.file == "-":
        return sys.stdin.read()

    if args.file:
        with open(args.file, "r", encoding="utf-8") as sql_file:
            return sql_file.read()

    return args.sql


def main() -> int:
    args = parse_args()
    connection_string = build_connection_string(args.server, args.database)

    if args.show_connection_string:
        print(connection_string, flush=True)

    sql = read_sql_from_args(args)

    try:
        columns, rows, rowcount = run_sql(sql, server=args.server, database=args.database)
    except Exception as exc:  # noqa: BLE001 - CLI should show driver errors.
        print(f"SQL execution failed: {exc}", file=sys.stderr)
        return 1

    if columns:
        print("\t".join(columns), flush=True)
        for row in rows:
            print("\t".join("" if value is None else str(value) for value in row), flush=True)
    else:
        print(f"Rows affected: {rowcount}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
