from pathlib import Path

import pytest

from source.ddlhelper import (
    build_create_table_sql_from_describe_rows,
    generate_infer_create_table_sql,
    generate_load_stored_procedure_sql,
    wrap_create_or_alter_view,
)


FABRIC_SAMPLE_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "fabric_sample_sql"


def _fabric_sample_fixture_sql(name):
    return (FABRIC_SAMPLE_FIXTURE_DIR / name).read_text()


def _materialise_installed_procedure(installer_sql, replacements):
    marker = "SET @weaver_proc_sql = N'"
    start = installer_sql.index(marker) + len(marker)
    end = installer_sql.index("';\nSET @weaver_proc_sql = REPLACE", start)
    procedure_sql = installer_sql[start:end].replace("''", "'")

    for placeholder, value in replacements.items():
        procedure_sql = procedure_sql.replace(placeholder, value)

    return procedure_sql


@pytest.mark.parametrize(
    "fixture_name",
    [
        "customer_order_summary.sql",
        "product_sales_union.sql",
        "recent_web_orders_cte.sql",
    ],
)
def test_generate_infer_create_table_sql_uses_sample_query_fixtures(fixture_name):
    sql = _fabric_sample_fixture_sql(fixture_name)
    transformed = generate_infer_create_table_sql(
        sql,
        "dbo.generated_from_sys",
        identity_column="GeneratedSK",
        primary_key_columns=["GeneratedSK"],
        temp_table_name="#weaver_shape_test",
    )
    shape_sql = transformed.split("DECLARE @weaver_identity_column", maxsplit=1)[0]

    assert "INTO #weaver_shape_test" in shape_sql
    assert "1=0" in shape_sql
    assert "CREATE TABLE [dbo].[generated_from_sys_Current]" in transformed
    assert "CREATE TABLE [dbo].[generated_from_sys_History]" in transformed
    assert "CREATE OR ALTER VIEW [dbo].[generated_from_sys]" in transformed
    assert "FROM tempdb.sys.columns AS c" in transformed
    assert "INNER JOIN tempdb.sys.types AS t" in transformed
    assert "OBJECT_ID(N'tempdb..#weaver_shape_test')" in transformed
    assert "sys.dm_exec_describe_first_result_set" not in transformed
    assert "QUOTENAME(@weaver_identity_column) + N' bigint IDENTITY NOT NULL'" in transformed
    assert "N'[Row delete datetime] datetime2(6) NOT NULL'" in transformed
    assert "(1, N'GeneratedSK')" in transformed
    assert "+ N'PRIMARY KEY NONCLUSTERED ('" in transformed
    assert "+ N') NOT ENFORCED;'" in transformed
    assert "EXEC sys.sp_executesql @weaver_current_create_sql;" in transformed
    assert "EXEC sys.sp_executesql @weaver_history_create_sql;" in transformed
    assert "EXEC sys.sp_executesql @weaver_current_pk_sql;" in transformed
    assert "EXEC sys.sp_executesql @weaver_view_sql;" in transformed


def test_generate_infer_create_table_sql_contains_yaml_type_mappings():
    transformed = generate_infer_create_table_sql(
        "SELECT CAST(N'x' AS nvarchar(50)) AS Name",
        "report.MixedTypes",
        temp_table_name="#weaver_shape_types",
    )

    assert "WHEN 'nvarchar' THEN N'varchar('" in transformed
    assert "WHEN 'nchar' THEN N'varchar('" in transformed
    assert "WHEN 'tinyint' THEN N'smallint'" in transformed
    assert "WHEN 'money' THEN N'decimal(' + N'19' + N',' + N'4' + N')'" in transformed
    assert "WHEN 'datetime' THEN N'datetime2(' + N'6' + N')'" in transformed
    assert "WHEN 'varbinary' THEN N'varbinary('" in transformed


def test_generate_infer_create_table_sql_quotes_target_and_identity_names():
    transformed = generate_infer_create_table_sql(
        "SELECT 1 AS Id",
        "audit.[Odd.Table Name]",
        identity_column="Table SK",
        temp_table_name="#weaver_shape_quote",
    )

    assert "CREATE TABLE [audit].[Odd.Table Name_Current]" in transformed
    assert "CREATE TABLE [audit].[Odd.Table Name_History]" in transformed
    assert "CREATE OR ALTER VIEW [audit].[Odd.Table Name]" in transformed
    assert "DECLARE @weaver_identity_column varchar(128) = N'Table SK';" in transformed
    assert "SELECT 1 AS Id INTO #weaver_shape_quote WHERE 1=0" in transformed


def test_generate_infer_create_table_sql_without_identity_uses_null_identity():
    transformed = generate_infer_create_table_sql(
        "SELECT 1 AS Id",
        "dbo.NoIdentity",
        temp_table_name="#weaver_shape_no_identity",
    )

    assert "DECLARE @weaver_identity_column varchar(128) = NULL;" in transformed
    assert "WHERE @weaver_identity_column IS NOT NULL" in transformed


def test_generate_infer_create_table_sql_disambiguates_duplicate_column_names():
    transformed = generate_infer_create_table_sql(
        "SELECT 1 AS SameName, 2 AS SameName, 3",
        "dbo.DuplicateColumns",
        temp_table_name="#weaver_shape_duplicates",
    )

    assert "COALESCE(NULLIF(c.name, ''), CONCAT('Column', c.column_id))" in transformed
    assert "COUNT(*) OVER (PARTITION BY column_name)" in transformed
    assert "ROW_NUMBER() OVER (PARTITION BY column_name ORDER BY column_ordinal)" in transformed


def test_build_create_table_sql_from_describe_rows_maps_fabric_types():
    rows = [
        {
            "is_hidden": False,
            "column_ordinal": 1,
            "name": "Name",
            "is_nullable": True,
            "system_type_name": "nvarchar(128)",
            "max_length": 256,
            "precision": 0,
            "scale": 0,
            "error_number": None,
        },
        {
            "is_hidden": False,
            "column_ordinal": 2,
            "name": "Amount",
            "is_nullable": False,
            "system_type_name": "money",
            "max_length": 8,
            "precision": 19,
            "scale": 4,
            "error_number": None,
        },
        {
            "is_hidden": False,
            "column_ordinal": 3,
            "name": "",
            "is_nullable": True,
            "system_type_name": "tinyint",
            "max_length": 1,
            "precision": 3,
            "scale": 0,
            "error_number": None,
        },
    ]

    assert build_create_table_sql_from_describe_rows(
        rows,
        "dbo.Inferred",
        identity_column="InferredSK",
        primary_key_columns=["InferredSK"],
    ) == (
        "CREATE TABLE [dbo].[Inferred_Current] (\n"
        "    [InferredSK] bigint IDENTITY NOT NULL,\n"
        "    [Name] varchar(128) NULL,\n"
        "    [Amount] decimal(19,4) NOT NULL,\n"
        "    [Column3] smallint NULL,\n"
        "    [Row insert datetime] datetime2(6) NULL,\n"
        "    [Row update datetime] datetime2(6) NULL,\n"
        "    [Row delete datetime] datetime2(6) NOT NULL\n"
        ");\n\n"
        "CREATE TABLE [dbo].[Inferred_History] (\n"
        "    [InferredSK] bigint NOT NULL,\n"
        "    [Name] varchar(128) NULL,\n"
        "    [Amount] decimal(19,4) NOT NULL,\n"
        "    [Column3] smallint NULL,\n"
        "    [Row insert datetime] datetime2(6) NULL,\n"
        "    [Row update datetime] datetime2(6) NULL,\n"
        "    [Row delete datetime] datetime2(6) NOT NULL\n"
        ");\n\n"
        "ALTER TABLE [dbo].[Inferred_Current] ADD CONSTRAINT [PK_Inferred_Current] "
        "PRIMARY KEY NONCLUSTERED ([InferredSK]) NOT ENFORCED;\n\n"
        "CREATE OR ALTER VIEW [dbo].[Inferred] AS\n"
        "SELECT\n"
        "    [InferredSK],\n"
        "    [Name],\n"
        "    [Amount],\n"
        "    [Column3],\n"
        "    [Row insert datetime],\n"
        "    [Row update datetime]\n"
        "FROM [dbo].[Inferred_Current];"
    )


def test_build_create_table_sql_from_describe_rows_disambiguates_duplicates():
    rows = [
        {
            "is_hidden": False,
            "column_ordinal": 1,
            "name": "Duplicate",
            "is_nullable": True,
            "system_type_name": "int",
            "max_length": 4,
            "precision": 10,
            "scale": 0,
            "error_number": None,
        },
        {
            "is_hidden": False,
            "column_ordinal": 2,
            "name": "Duplicate",
            "is_nullable": True,
            "system_type_name": "int",
            "max_length": 4,
            "precision": 10,
            "scale": 0,
            "error_number": None,
        },
    ]

    create_sql = build_create_table_sql_from_describe_rows(rows, "dbo.Duplicates")

    assert "[Duplicate_1] int NULL" in create_sql
    assert "[Duplicate_2] int NULL" in create_sql


def test_build_create_table_sql_from_describe_rows_adds_pk_to_current_only():
    rows = [
        {
            "is_hidden": False,
            "column_ordinal": 1,
            "name": "BusinessKey",
            "is_nullable": True,
            "system_type_name": "varchar(20)",
            "max_length": 20,
            "precision": 0,
            "scale": 0,
            "error_number": None,
        },
        {
            "is_hidden": False,
            "column_ordinal": 2,
            "name": "Value",
            "is_nullable": True,
            "system_type_name": "int",
            "max_length": 4,
            "precision": 10,
            "scale": 0,
            "error_number": None,
        },
    ]

    create_sql = build_create_table_sql_from_describe_rows(
        rows,
        "warehouse.Customer",
        primary_key_columns=["BusinessKey"],
    )

    current_section = create_sql.split("CREATE TABLE [warehouse].[Customer_History]", maxsplit=1)[0]
    history_section = create_sql.split("CREATE TABLE [warehouse].[Customer_History]", maxsplit=1)[1].split("ALTER TABLE", maxsplit=1)[0]
    view_section = create_sql.split("CREATE OR ALTER VIEW [warehouse].[Customer] AS", maxsplit=1)[1]

    assert "[BusinessKey] varchar(20) NOT NULL" in current_section
    assert "ALTER TABLE [warehouse].[Customer_Current] ADD CONSTRAINT [PK_Customer_Current] PRIMARY KEY NONCLUSTERED ([BusinessKey]) NOT ENFORCED;" in create_sql
    assert "PRIMARY KEY" not in history_section.split("CREATE OR ALTER VIEW", maxsplit=1)[0]
    assert "[Row delete datetime]" not in view_section


def test_build_create_table_sql_from_describe_rows_requires_schema_table_name():
    rows = [
        {
            "is_hidden": False,
            "column_ordinal": 1,
            "name": "Id",
            "is_nullable": False,
            "system_type_name": "int",
            "max_length": 4,
            "precision": 10,
            "scale": 0,
            "error_number": None,
        },
    ]

    with pytest.raises(ValueError, match="schema.table"):
        build_create_table_sql_from_describe_rows(rows, "NoSchema")


def test_generate_load_stored_procedure_sql_builds_pk_incremental_loader():
    installer_sql = generate_load_stored_procedure_sql(
        "SELECT CustomerCode, CustomerName, Balance FROM dbo.SourceCustomers",
        "mart.Customer",
        primary_key_columns=["CustomerCode"],
    )
    installer_prefix = installer_sql.split("SET @weaver_proc_sql", maxsplit=1)[0]

    assert installer_sql.startswith("/* Weaver generated ETL procedure installer. */")
    assert "OBJECT_ID(N'[mart].[Customer_Current]')" in installer_prefix
    assert "CREATE TABLE [mart].[Customer_Staging] AS" not in installer_prefix
    assert "SET @weaver_proc_sql = N'CREATE OR ALTER PROCEDURE [_].[ETL mart.Customer]" in installer_sql
    assert "CREATE TABLE [mart].[Customer_Staging] AS" in installer_sql
    assert "CREATE TABLE [mart].[Customer_Upsert] AS" in installer_sql
    assert "LEFT JOIN [mart].[Customer] AS t\n        ON s.[CustomerCode] = t.[CustomerCode]" in installer_sql
    assert "OR EXISTS (" in installer_sql
    assert "EXCEPT" in installer_sql
    assert "AS [_Is new row]" in installer_sql
    assert "INSERT INTO [mart].[Customer_Current]" in installer_sql
    assert "INSERT INTO [mart].[Customer_History]" in installer_sql
    assert "UPDATE c" in installer_sql
    assert "DELETE c" in installer_sql
    assert "WHERE u.[_Is new row] = 1" in installer_sql
    assert "WHERE u.[_Is new row] = 0" in installer_sql
    assert "c.[Row update datetime] = @weaver_load_datetime" in installer_sql
    assert "WHEN c.name = N'Row delete datetime' THEN N'@weaver_load_datetime AS ' + QUOTENAME(c.name)" in installer_sql
    assert "DELETE h" in installer_sql
    assert "SET @weaver_proc_sql = REPLACE(@weaver_proc_sql, N'__SOURCE_COLUMNS__', @weaver_source_columns);" in installer_sql

    assert _materialise_installed_procedure(
        installer_sql,
        {
            "__SOURCE_COLUMNS__": "[CustomerCode], [CustomerName], [Balance]",
            "__STAGING_SELECT_COLUMNS__": "s.[CustomerCode], s.[CustomerName], s.[Balance]",
            "__UPSERT_SELECT_COLUMNS__": "u.[CustomerCode], u.[CustomerName], u.[Balance]",
            "__TARGET_SELECT_COLUMNS__": "t.[CustomerCode], t.[CustomerName], t.[Balance]",
            "__HISTORY_COLUMNS__": "[CustomerCode], [CustomerName], [Balance], [Row insert datetime], [Row update datetime], [Row delete datetime]",
            "__HISTORY_SELECT_COLUMNS__": "c.[CustomerCode], c.[CustomerName], c.[Balance], c.[Row insert datetime], c.[Row update datetime], @weaver_load_datetime AS [Row delete datetime]",
            "__UPDATE_SET_COLUMNS__": "c.[CustomerName] = u.[CustomerName], c.[Balance] = u.[Balance], c.[Row update datetime] = @weaver_load_datetime, c.[Row delete datetime] = CONVERT(datetime2(6), '9999-12-31 00:00:00')",
        },
    ) == """CREATE OR ALTER PROCEDURE [_].[ETL mart.Customer]
AS
BEGIN
    SET NOCOUNT ON;
    DECLARE @weaver_load_datetime datetime2(6) = SYSUTCDATETIME();

    IF OBJECT_ID(N'[mart].[Customer_Upsert]', N'U') IS NOT NULL DROP TABLE [mart].[Customer_Upsert];
    IF OBJECT_ID(N'[mart].[Customer_Staging]', N'U') IS NOT NULL DROP TABLE [mart].[Customer_Staging];

    CREATE TABLE [mart].[Customer_Staging] AS
    SELECT CustomerCode, CustomerName, Balance FROM dbo.SourceCustomers;

    CREATE TABLE [mart].[Customer_Upsert] AS
    SELECT
        s.[CustomerCode], s.[CustomerName], s.[Balance],
        CASE WHEN t.[CustomerCode] IS NULL THEN CAST(1 AS int) ELSE CAST(0 AS int) END AS [_Is new row]
    FROM [mart].[Customer_Staging] AS s
    LEFT JOIN [mart].[Customer] AS t
        ON s.[CustomerCode] = t.[CustomerCode]
    WHERE t.[CustomerCode] IS NULL
        OR EXISTS (
            SELECT
                s.[CustomerCode], s.[CustomerName], s.[Balance]
            EXCEPT
            SELECT
                t.[CustomerCode], t.[CustomerName], t.[Balance]
        );

    INSERT INTO [mart].[Customer_Current] (
        [CustomerCode], [CustomerName], [Balance],
        [Row insert datetime],
        [Row update datetime],
        [Row delete datetime]
    )
    SELECT
        u.[CustomerCode], u.[CustomerName], u.[Balance],
        @weaver_load_datetime,
        @weaver_load_datetime,
        CONVERT(datetime2(6), '9999-12-31 00:00:00')
    FROM [mart].[Customer_Upsert] AS u
    WHERE u.[_Is new row] = 1;

    BEGIN TRY
        INSERT INTO [mart].[Customer_History] (
            [CustomerCode], [CustomerName], [Balance], [Row insert datetime], [Row update datetime], [Row delete datetime]
        )
        SELECT
            c.[CustomerCode], c.[CustomerName], c.[Balance], c.[Row insert datetime], c.[Row update datetime], @weaver_load_datetime AS [Row delete datetime]
        FROM [mart].[Customer_Current] AS c
        INNER JOIN [mart].[Customer_Upsert] AS u
            ON c.[CustomerCode] = u.[CustomerCode]
        WHERE u.[_Is new row] = 0;

        UPDATE c
        SET c.[CustomerName] = u.[CustomerName], c.[Balance] = u.[Balance], c.[Row update datetime] = @weaver_load_datetime, c.[Row delete datetime] = CONVERT(datetime2(6), '9999-12-31 00:00:00')
        FROM [mart].[Customer_Current] AS c
        INNER JOIN [mart].[Customer_Upsert] AS u
            ON c.[CustomerCode] = u.[CustomerCode]
        WHERE u.[_Is new row] = 0;
    END TRY
    BEGIN CATCH
        DELETE h
        FROM [mart].[Customer_History] AS h
        INNER JOIN [mart].[Customer_Upsert] AS u
            ON h.[CustomerCode] = u.[CustomerCode]
        WHERE u.[_Is new row] = 0
            AND h.[Row delete datetime] = @weaver_load_datetime;

        THROW;
    END CATCH;

    BEGIN TRY
        INSERT INTO [mart].[Customer_History] (
            [CustomerCode], [CustomerName], [Balance], [Row insert datetime], [Row update datetime], [Row delete datetime]
        )
        SELECT
            c.[CustomerCode], c.[CustomerName], c.[Balance], c.[Row insert datetime], c.[Row update datetime], @weaver_load_datetime AS [Row delete datetime]
        FROM [mart].[Customer_Current] AS c
        WHERE NOT EXISTS (SELECT 1 FROM [mart].[Customer_Staging] AS s WHERE s.[CustomerCode] = c.[CustomerCode]);

        DELETE c
        FROM [mart].[Customer_Current] AS c
        WHERE NOT EXISTS (SELECT 1 FROM [mart].[Customer_Staging] AS s WHERE s.[CustomerCode] = c.[CustomerCode]);
    END TRY
    BEGIN CATCH
        DELETE h
        FROM [mart].[Customer_History] AS h
        WHERE h.[Row delete datetime] = @weaver_load_datetime
            AND NOT EXISTS (SELECT 1 FROM [mart].[Customer_Staging] AS s WHERE s.[CustomerCode] = h.[CustomerCode]);

        THROW;
    END CATCH;

    IF OBJECT_ID(N'[mart].[Customer_Upsert]', N'U') IS NOT NULL DROP TABLE [mart].[Customer_Upsert];
    IF OBJECT_ID(N'[mart].[Customer_Staging]', N'U') IS NOT NULL DROP TABLE [mart].[Customer_Staging];
END;"""


def test_generate_load_stored_procedure_sql_builds_full_refresh_without_pk():
    installer_sql = generate_load_stored_procedure_sql(
        "SELECT ProductCode, ProductName FROM dbo.SourceProducts;",
        "mart.Product",
    )

    assert "SET @weaver_proc_sql = N'CREATE OR ALTER PROCEDURE [_].[ETL mart.Product]" in installer_sql
    assert "OBJECT_ID(N'[mart].[Product_Current]')" in installer_sql
    assert "CREATE TABLE [mart].[Product_Staging] AS" in installer_sql
    assert "CREATE TABLE [mart].[Product_Upsert] AS" not in installer_sql
    assert "DELETE FROM [mart].[Product_Current];" in installer_sql
    assert "INSERT INTO [mart].[Product_Current]" in installer_sql
    assert "INSERT INTO [mart].[Product_History]" not in installer_sql
    assert "[Row insert datetime]," in installer_sql
    assert "[Row update datetime]," in installer_sql
    assert "[Row delete datetime]" in installer_sql

    assert _materialise_installed_procedure(
        installer_sql,
        {
            "__SOURCE_COLUMNS__": "[ProductCode], [ProductName]",
            "__STAGING_SELECT_COLUMNS__": "s.[ProductCode], s.[ProductName]",
            "__UPSERT_SELECT_COLUMNS__": "",
            "__TARGET_SELECT_COLUMNS__": "",
            "__HISTORY_COLUMNS__": "[ProductCode], [ProductName], [Row insert datetime], [Row update datetime], [Row delete datetime]",
            "__HISTORY_SELECT_COLUMNS__": "",
            "__UPDATE_SET_COLUMNS__": "",
        },
    ) == """CREATE OR ALTER PROCEDURE [_].[ETL mart.Product]
AS
BEGIN
    SET NOCOUNT ON;
    DECLARE @weaver_load_datetime datetime2(6) = SYSUTCDATETIME();

    IF OBJECT_ID(N'[mart].[Product_Upsert]', N'U') IS NOT NULL DROP TABLE [mart].[Product_Upsert];
    IF OBJECT_ID(N'[mart].[Product_Staging]', N'U') IS NOT NULL DROP TABLE [mart].[Product_Staging];

    CREATE TABLE [mart].[Product_Staging] AS
    SELECT ProductCode, ProductName FROM dbo.SourceProducts;

    DELETE FROM [mart].[Product_Current];

    INSERT INTO [mart].[Product_Current] (
        [ProductCode], [ProductName],
        [Row insert datetime],
        [Row update datetime],
        [Row delete datetime]
    )
    SELECT
        s.[ProductCode], s.[ProductName],
        @weaver_load_datetime,
        @weaver_load_datetime,
        CONVERT(datetime2(6), '9999-12-31 00:00:00')
    FROM [mart].[Product_Staging] AS s;

    IF OBJECT_ID(N'[mart].[Product_Upsert]', N'U') IS NOT NULL DROP TABLE [mart].[Product_Upsert];
    IF OBJECT_ID(N'[mart].[Product_Staging]', N'U') IS NOT NULL DROP TABLE [mart].[Product_Staging];
END;"""


def test_generate_load_stored_procedure_sql_quotes_names_and_composite_pk():
    installer_sql = generate_load_stored_procedure_sql(
        "SELECT [Order Id], [Line No], Amount FROM dbo.SourceOrderLines",
        "audit.[Order Line]",
        primary_key_columns=["Order Id", "Line No"],
    )

    assert "CREATE OR ALTER PROCEDURE [_].[ETL audit.Order Line]" in installer_sql
    assert "CREATE TABLE [audit].[Order Line_Staging] AS" in installer_sql
    assert (
        "LEFT JOIN [audit].[Order Line] AS t\n        ON "
        "s.[Order Id] = t.[Order Id] AND s.[Line No] = t.[Line No]"
    ) in installer_sql
    assert "N'order id'" in installer_sql
    assert "N'line no'" in installer_sql


def test_wrap_create_or_alter_view_for_plain_select():
    assert wrap_create_or_alter_view(
        "SELECT customer_id FROM dbo.weaver_fixture_customers;",
        "report.CustomerView",
    ) == (
        "CREATE OR ALTER VIEW [report].[CustomerView] AS\n"
        "SELECT customer_id FROM dbo.weaver_fixture_customers"
    )


def test_wrap_create_or_alter_view_for_cte():
    sql = """WITH active_customers AS (
    SELECT
        customer_id
    FROM dbo.weaver_fixture_customers
    WHERE is_active = 1
)
SELECT
    customer_id
FROM active_customers;
"""

    assert wrap_create_or_alter_view(sql, "dbo.ActiveCustomers") == """CREATE OR ALTER VIEW [dbo].[ActiveCustomers] AS
WITH active_customers AS (
    SELECT
        customer_id
    FROM dbo.weaver_fixture_customers
    WHERE is_active = 1
)
SELECT
    customer_id
FROM active_customers"""


def test_wrap_create_or_alter_view_strips_leading_semicolon_before_cte():
    assert wrap_create_or_alter_view(
        ";WITH cte AS (SELECT 1 AS id) SELECT id FROM cte;",
        "[dbo].[CteView]",
    ) == (
        "CREATE OR ALTER VIEW [dbo].[CteView] AS\n"
        "WITH cte AS (SELECT 1 AS id) SELECT id FROM cte"
    )


def test_wrap_create_or_alter_view_quotes_odd_view_name():
    assert wrap_create_or_alter_view(
        "SELECT 1 AS id",
        "report.[Odd.View Name]",
    ).startswith("CREATE OR ALTER VIEW [report].[Odd.View Name] AS")
