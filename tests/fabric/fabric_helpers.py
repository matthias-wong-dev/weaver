"""Helpers shared by opt-in Fabric integration tests.

These talk to a real Fabric Warehouse / Lakehouse and are only imported by tests
under ``tests/fabric`` (deselected by default). Object/schema names are generic.
"""

from __future__ import annotations

import time

from weaver_runtime.dbrep.sql.connection import connect, execute_script, query

TEST_SCHEMAS = ("raw", "mart", "report")


def wait_queryable(server: str, database: str, timeout: float = 180.0) -> None:
    """Block until a freshly created warehouse's SQL endpoint answers a query."""

    deadline = time.time() + timeout
    last_error: Exception | None = None
    while time.time() < deadline:
        try:
            with connect(server, database, timeout=30) as conn:
                query(conn, "select 1 as ok")
            return
        except Exception as exc:  # noqa: BLE001 - endpoint warmup surfaces varied errors
            last_error = exc
            time.sleep(10)
    raise RuntimeError(f"warehouse {database!r} did not become queryable: {last_error}")


def object_exists(server: str, database: str, quoted_name: str, object_type: str) -> bool:
    literal = quoted_name.replace("'", "''")
    with connect(server, database) as conn:
        rows = query(
            conn,
            f"select 1 as present from sys.objects "
            f"where object_id = object_id(N'{literal}', N'{object_type}')",
        )
    return bool(rows)


def view_rows(server: str, database: str, view: str) -> list[dict]:
    with connect(server, database) as conn:
        return query(conn, f"select * from {view}")


def manifest_ids(server: str, database: str) -> list[str]:
    with connect(server, database) as conn:
        if not query(
            conn,
            "select 1 as present from sys.objects where object_id = object_id(N'_weaver.objects')",
        ):
            return []
        return [
            row["object_id"]
            for row in query(conn, "select object_id from _weaver.objects order by object_id")
        ]


def reset(server: str, database: str) -> None:
    """Drop Weaver-managed test objects so a run starts and ends clean."""

    with connect(server, database) as conn:
        for row in query(
            conn,
            "select table_schema as s, table_name as n from INFORMATION_SCHEMA.VIEWS "
            "where table_schema in ('raw', 'mart', 'report')",
        ):
            execute_script(conn, f"drop view [{row['s']}].[{row['n']}];")

        for row in query(
            conn,
            "select name from sys.procedures where schema_name(schema_id) = '_' "
            "and (name like 'ETL raw.%' or name like 'ETL mart.%' or name like 'ETL report.%')",
        ):
            execute_script(conn, f"drop procedure [_].[{row['name']}];")

        for row in query(
            conn,
            "select table_schema as s, table_name as n from INFORMATION_SCHEMA.TABLES "
            "where table_type = 'BASE TABLE' and table_schema in ('raw', 'mart', 'report')",
        ):
            execute_script(conn, f"drop table [{row['s']}].[{row['n']}];")

        execute_script(conn, "if object_id(N'_weaver.objects', N'U') is not null drop table _weaver.objects;")
