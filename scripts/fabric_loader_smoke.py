"""Smoke test the generated Fabric loader procedure against a live warehouse."""

from __future__ import annotations

from pathlib import Path
from decimal import Decimal
import sys
import uuid

import sqlparse


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from source.ddlhelper import (  # noqa: E402
    build_create_table_sql_from_describe_rows,
    generate_load_stored_procedure_sql,
)
from source.fabric_sql import run_sql  # noqa: E402


def run_batches(sql: str) -> None:
    for statement in sqlparse.split(sql):
        if statement.strip():
            try:
                run_sql(statement)
            except Exception as exc:
                print(f"failed batch:\n{statement}", file=sys.stderr)
                raise exc


def main() -> None:
    suffix = uuid.uuid4().hex[:8]
    source_table = f"[dbo].[weaver_loader_source_{suffix}]"
    target_name = f"dbo.weaver_loader_target_{suffix}"
    target_base = f"weaver_loader_target_{suffix}"
    proc_name = f"[_].[ETL dbo.{target_base}]"

    cleanup_statements = [
        f"IF OBJECT_ID(N'{proc_name}', N'P') IS NOT NULL DROP PROCEDURE {proc_name};",
        f"IF OBJECT_ID(N'[dbo].[{target_base}]', N'V') IS NOT NULL DROP VIEW [dbo].[{target_base}];",
        f"IF OBJECT_ID(N'[dbo].[{target_base}_Upsert]', N'U') IS NOT NULL DROP TABLE [dbo].[{target_base}_Upsert];",
        f"IF OBJECT_ID(N'[dbo].[{target_base}_Staging]', N'U') IS NOT NULL DROP TABLE [dbo].[{target_base}_Staging];",
        f"IF OBJECT_ID(N'[dbo].[{target_base}_History]', N'U') IS NOT NULL DROP TABLE [dbo].[{target_base}_History];",
        f"IF OBJECT_ID(N'[dbo].[{target_base}_Current]', N'U') IS NOT NULL DROP TABLE [dbo].[{target_base}_Current];",
        f"IF OBJECT_ID(N'{source_table}', N'U') IS NOT NULL DROP TABLE {source_table};",
    ]

    try:
        for statement in cleanup_statements:
            run_sql(statement)

        run_sql("IF SCHEMA_ID(N'_') IS NULL EXEC(N'CREATE SCHEMA [_]');")
        run_sql(
            f"""CREATE TABLE {source_table} (
    [CustomerCode] varchar(20) NOT NULL,
    [CustomerName] varchar(80) NULL,
    [Balance] decimal(10,2) NULL
);"""
        )
        run_sql(
            f"""INSERT INTO {source_table} ([CustomerCode], [CustomerName], [Balance])
VALUES
    ('C001', 'Ada', 10.00),
    ('C002', 'Grace', 20.00);"""
        )

        target_ddl = build_create_table_sql_from_describe_rows(
            [
                {
                    "is_hidden": False,
                    "column_ordinal": 1,
                    "name": "CustomerCode",
                    "is_nullable": False,
                    "system_type_name": "varchar(20)",
                    "max_length": 20,
                    "precision": 0,
                    "scale": 0,
                    "error_number": None,
                },
                {
                    "is_hidden": False,
                    "column_ordinal": 2,
                    "name": "CustomerName",
                    "is_nullable": True,
                    "system_type_name": "varchar(80)",
                    "max_length": 80,
                    "precision": 0,
                    "scale": 0,
                    "error_number": None,
                },
                {
                    "is_hidden": False,
                    "column_ordinal": 3,
                    "name": "Balance",
                    "is_nullable": True,
                    "system_type_name": "decimal(10,2)",
                    "max_length": 9,
                    "precision": 10,
                    "scale": 2,
                    "error_number": None,
                },
            ],
            target_name,
            primary_key_columns=["CustomerCode"],
        )
        run_batches(target_ddl)

        procedure_sql = generate_load_stored_procedure_sql(
            f"SELECT [CustomerCode], [CustomerName], [Balance] FROM {source_table}",
            target_name,
            primary_key_columns=["CustomerCode"],
        )
        run_sql(procedure_sql)

        run_sql(f"EXEC {proc_name};")
        first_counts = run_sql(
            f"""SELECT
    (SELECT COUNT(*) FROM [dbo].[{target_base}_Current]) AS CurrentCount,
    (SELECT COUNT(*) FROM [dbo].[{target_base}_History]) AS HistoryCount;"""
        )
        assert first_counts.rows == [(2, 0)], first_counts.rows

        run_sql(
            f"""UPDATE {source_table}
SET [CustomerName] = 'Ada Lovelace', [Balance] = 15.50
WHERE [CustomerCode] = 'C001';"""
        )
        run_sql(f"DELETE FROM {source_table} WHERE [CustomerCode] = 'C002';")
        run_sql(
            f"""INSERT INTO {source_table} ([CustomerCode], [CustomerName], [Balance])
VALUES ('C003', 'Katherine', 30.00);"""
        )

        run_sql(f"EXEC {proc_name};")
        second_counts = run_sql(
            f"""SELECT
    (SELECT COUNT(*) FROM [dbo].[{target_base}_Current]) AS CurrentCount,
    (SELECT COUNT(*) FROM [dbo].[{target_base}_History]) AS HistoryCount;"""
        )
        assert second_counts.rows == [(2, 2)], second_counts.rows

        final_rows = run_sql(
            f"""SELECT [CustomerCode], [CustomerName], CONVERT(decimal(10,2), [Balance]) AS Balance
FROM [dbo].[{target_base}_Current]
ORDER BY [CustomerCode];"""
        )
        assert final_rows.rows == [
            ("C001", "Ada Lovelace", Decimal("15.50")),
            ("C003", "Katherine", Decimal("30.00")),
        ], final_rows.rows

        invariant = run_sql(
            f"""SELECT COUNT(*) AS MismatchCount
FROM [dbo].[{target_base}_History] AS h
INNER JOIN [dbo].[{target_base}_Current] AS c
    ON c.[CustomerCode] = h.[CustomerCode]
WHERE h.[CustomerCode] = 'C001'
    AND h.[Row delete datetime] <> c.[Row update datetime];"""
        )
        assert invariant.rows == [(0,)], invariant.rows

        print(f"Fabric loader smoke passed for {target_name}")
    finally:
        for statement in cleanup_statements:
            try:
                run_sql(statement)
            except Exception as exc:  # pragma: no cover - cleanup best effort
                print(f"cleanup failed: {statement}\n{exc}", file=sys.stderr)


if __name__ == "__main__":
    main()
