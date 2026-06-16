from pathlib import Path

import pytest

from source.ddlhelper import (
    build_create_table_sql_from_describe_rows,
    generate_infer_create_table_sql,
    wrap_create_or_alter_view,
)


FABRIC_SAMPLE_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "fabric_sample_sql"


def _fabric_sample_fixture_sql(name):
    return (FABRIC_SAMPLE_FIXTURE_DIR / name).read_text()


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
    assert "N'[Row delete datetime] datetime2(7) NOT NULL DEFAULT ''9999-12-31 00:00:00''" in transformed
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
        "    [Row insert datetime] datetime2(7) NULL,\n"
        "    [Row update datetime] datetime2(7) NULL,\n"
        "    [Row delete datetime] datetime2(7) NOT NULL DEFAULT '9999-12-31 00:00:00'\n"
        ");\n\n"
        "CREATE TABLE [dbo].[Inferred_History] (\n"
        "    [InferredSK] bigint NOT NULL,\n"
        "    [Name] varchar(128) NULL,\n"
        "    [Amount] decimal(19,4) NOT NULL,\n"
        "    [Column3] smallint NULL,\n"
        "    [Row insert datetime] datetime2(7) NULL,\n"
        "    [Row update datetime] datetime2(7) NULL,\n"
        "    [Row delete datetime] datetime2(7) NOT NULL\n"
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
