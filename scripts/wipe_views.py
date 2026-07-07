#!/usr/bin/env python3
"""Drop existing SES views from Fabric/Azure SQL schemas."""

from __future__ import annotations

import argparse
import sys

from sqlserver import connect


def drop_existing_views(cursor) -> None:
    statement = f"""
DECLARE @sql nvarchar(max);

SELECT @sql = STRING_AGG(
    CONVERT(
        nvarchar(max),
        N'DROP VIEW ' + QUOTENAME(s.[name]) + N'.' + QUOTENAME(v.[name])
    ),
    N';' + NCHAR(10)
) WITHIN GROUP (ORDER BY s.[name], v.[name])
FROM sys.views v
INNER JOIN sys.schemas s ON s.[schema_id] = v.[schema_id]
where s.name not in 
('dbo',
'guest',
'INFORMATION_SCHEMA',
'sys',
'db_owner',
'db_accessadmin',
'db_securityadmin',
'db_ddladmin',
'db_backupoperator',
'db_datareader',
'db_datawriter',
'db_denydatareader',
'db_denydatawriter',
'queryinsights'
)

IF @sql IS NOT NULL
BEGIN
    SET @sql = @sql + N';';
    EXEC sp_executesql @sql;
END
"""
    cursor.execute(statement)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--schema",
        action="append",
        dest="schemas",
        help=(
            "Schema to wipe. Can be provided more than once. Defaults to schemas "
            "referenced by SES View ID metadata."
        ),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    

    with connect() as conn:
        cursor = conn.cursor()
        drop_existing_views(cursor)
        conn.commit()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
