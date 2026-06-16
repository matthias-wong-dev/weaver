from pathlib import Path

import pytest
import sqlparse
from sqlparse import tokens as T

from source.sqlwrangle import (
    build_create_table_sql_from_describe_rows,
    generate_infer_create_table_sql,
    insert_ctas,
    insert_select_into,
    insert_where_one_eq_zero,
)


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "sql"
FABRIC_SAMPLE_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "fabric_sample_sql"


def _fixture_sql(name):
    return (FIXTURE_DIR / name).read_text()


def _fabric_sample_fixture_sql(name):
    return (FABRIC_SAMPLE_FIXTURE_DIR / name).read_text()


def _select_count(sql):
    return sum(
        1
        for statement in sqlparse.parse(sql)
        for token in statement.flatten()
        if token.ttype is T.DML and token.normalized.upper() == "SELECT"
    )


@pytest.mark.parametrize(
    "fixture_name",
    [
        "customer_retention_cohort.sql",
        "inventory_replenishment_forecast.sql",
        "order_fulfillment_pipeline.sql",
        "revenue_reconciliation_month_end.sql",
        "security_access_audit.sql",
    ],
)
def test_insert_where_one_eq_zero_guards_every_select_in_serious_sql_fixtures(fixture_name):
    sql = _fixture_sql(fixture_name)
    transformed = insert_where_one_eq_zero(sql)

    assert transformed.count("1=0") == _select_count(sql)
    assert "SELECT * FROM dbo.Nope" not in transformed


@pytest.mark.parametrize(
    "fixture_name",
    [
        "customer_retention_cohort.sql",
        "inventory_replenishment_forecast.sql",
        "order_fulfillment_pipeline.sql",
        "revenue_reconciliation_month_end.sql",
        "security_access_audit.sql",
    ],
)
def test_insert_ctas_wraps_one_query_in_serious_sql_fixtures(fixture_name):
    sql = _fixture_sql(fixture_name)
    transformed = insert_ctas(sql, "dbo.ctas_result")

    assert transformed.count("CREATE TABLE dbo.ctas_result AS") == 1
    assert _select_count(transformed) == _select_count(sql)


@pytest.mark.parametrize(
    "fixture_name",
    [
        "customer_retention_cohort.sql",
        "inventory_replenishment_forecast.sql",
        "order_fulfillment_pipeline.sql",
        "revenue_reconciliation_month_end.sql",
        "security_access_audit.sql",
    ],
)
def test_insert_select_into_wraps_one_query_in_serious_sql_fixtures(fixture_name):
    sql = _fixture_sql(fixture_name)
    transformed = insert_select_into(sql, "dbo.select_into_result")

    assert transformed.count("INTO dbo.select_into_result") == 1
    assert transformed.count("CREATE TABLE dbo.select_into_result AS") == 0
    assert _select_count(transformed) == _select_count(sql)


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
        temp_table_name="#weaver_shape_test",
    )
    shape_sql = transformed.split("DECLARE @weaver_identity_column", maxsplit=1)[0]

    assert "INTO #weaver_shape_test" in shape_sql
    assert "1=0" in shape_sql
    assert "CREATE TABLE [dbo].[generated_from_sys]" in transformed
    assert "FROM tempdb.sys.columns AS c" in transformed
    assert "INNER JOIN tempdb.sys.types AS t" in transformed
    assert "OBJECT_ID(N'tempdb..#weaver_shape_test')" in transformed
    assert "sys.dm_exec_describe_first_result_set" not in transformed
    assert "QUOTENAME(@weaver_identity_column) + N' bigint IDENTITY NOT NULL'" in transformed
    assert "EXEC sys.sp_executesql @weaver_create_sql;" in transformed


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

    assert "CREATE TABLE [audit].[Odd.Table Name]" in transformed
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
    ) == (
        "CREATE TABLE [dbo].[Inferred] (\n"
        "    [InferredSK] bigint IDENTITY NOT NULL,\n"
        "    [Name] varchar(128) NULL,\n"
        "    [Amount] decimal(19,4) NOT NULL,\n"
        "    [Column3] smallint NULL\n"
        ");"
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


def test_insert_ctas_prefixes_simple_select():
    assert (
        insert_ctas("SELECT * FROM dbo.Users", "dbo.UsersCopy")
        == "CREATE TABLE dbo.UsersCopy AS\nSELECT * FROM dbo.Users"
    )


def test_insert_select_into_modifies_simple_select():
    assert (
        insert_select_into("SELECT * FROM dbo.Users", "dbo.UsersCopy")
        == "SELECT * INTO dbo.UsersCopy FROM dbo.Users"
    )


def test_insert_ctas_prefixes_only_last_standalone_select():
    sql = """SELECT *
FROM dbo.Users;

DECLARE @x int = 1;

SELECT *
FROM dbo.Teams
"""

    assert (
        insert_ctas(sql, "dbo.LastResult")
        == """SELECT *
FROM dbo.Users;

DECLARE @x int = 1;

CREATE TABLE dbo.LastResult AS
SELECT *
FROM dbo.Teams
"""
    )


def test_insert_select_into_modifies_only_last_standalone_select():
    sql = """SELECT *
FROM dbo.Users;

DECLARE @x int = 1;

SELECT *
FROM dbo.Teams
"""

    assert (
        insert_select_into(sql, "dbo.LastResult")
        == """SELECT *
FROM dbo.Users;

DECLARE @x int = 1;

SELECT *
INTO dbo.LastResult
FROM dbo.Teams
"""
    )


def test_insert_ctas_prefixes_cte_before_with():
    sql = """DECLARE @cutoff date = '2026-01-01';

WITH recent_users AS (
    SELECT
        u.Id
    FROM dbo.Users AS u
    WHERE
        u.CreatedAt >= @cutoff
)
SELECT
    ru.Id
FROM recent_users AS ru
"""

    assert (
        insert_ctas(sql, "dbo.RecentUsers")
        == """DECLARE @cutoff date = '2026-01-01';

CREATE TABLE dbo.RecentUsers AS
WITH recent_users AS (
    SELECT
        u.Id
    FROM dbo.Users AS u
    WHERE
        u.CreatedAt >= @cutoff
)
SELECT
    ru.Id
FROM recent_users AS ru
"""
    )


def test_insert_select_into_modifies_outer_cte_select():
    sql = """DECLARE @cutoff date = '2026-01-01';

WITH recent_users AS (
    SELECT
        u.Id
    FROM dbo.Users AS u
    WHERE
        u.CreatedAt >= @cutoff
)
SELECT
    ru.Id
FROM recent_users AS ru
"""

    assert (
        insert_select_into(sql, "dbo.RecentUsers")
        == """DECLARE @cutoff date = '2026-01-01';

WITH recent_users AS (
    SELECT
        u.Id
    FROM dbo.Users AS u
    WHERE
        u.CreatedAt >= @cutoff
)
SELECT
    ru.Id
INTO dbo.RecentUsers
FROM recent_users AS ru
"""
    )


def test_insert_ctas_wraps_entire_union_query():
    sql = """SELECT
    Id
FROM dbo.Users
UNION ALL
SELECT
    Id
FROM dbo.ArchivedUsers
"""

    assert (
        insert_ctas(sql, "dbo.AllUsers")
        == """CREATE TABLE dbo.AllUsers AS
SELECT
    Id
FROM dbo.Users
UNION ALL
SELECT
    Id
FROM dbo.ArchivedUsers
"""
    )


def test_insert_select_into_modifies_first_branch_of_union_query():
    sql = """SELECT
    Id
FROM dbo.Users
UNION ALL
SELECT
    Id
FROM dbo.ArchivedUsers
"""

    assert (
        insert_select_into(sql, "dbo.AllUsers")
        == """SELECT
    Id
INTO dbo.AllUsers
FROM dbo.Users
UNION ALL
SELECT
    Id
FROM dbo.ArchivedUsers
"""
    )


def test_insert_ctas_ignores_insert_select_and_uses_last_result_select():
    sql = """INSERT INTO #UserIds (Id)
SELECT
    u.Id
FROM dbo.Users AS u

SELECT
    t.Id
FROM dbo.Teams AS t
"""

    assert (
        insert_ctas(sql, "dbo.TeamResult")
        == """INSERT INTO #UserIds (Id)
SELECT
    u.Id
FROM dbo.Users AS u

CREATE TABLE dbo.TeamResult AS
SELECT
    t.Id
FROM dbo.Teams AS t
"""
    )


def test_insert_select_into_ignores_insert_select_and_uses_last_result_select():
    sql = """INSERT INTO #UserIds (Id)
SELECT
    u.Id
FROM dbo.Users AS u

SELECT
    t.Id
FROM dbo.Teams AS t
"""

    assert (
        insert_select_into(sql, "dbo.TeamResult")
        == """INSERT INTO #UserIds (Id)
SELECT
    u.Id
FROM dbo.Users AS u

SELECT
    t.Id
INTO dbo.TeamResult
FROM dbo.Teams AS t
"""
    )


def test_insert_ctas_handles_declarations_before_final_select_without_semicolon():
    sql = """DECLARE @cutoff date = '2026-01-01'
SET @cutoff = DATEADD(day, -7, @cutoff)
SELECT
    u.Id
FROM dbo.Users AS u
WHERE
    u.CreatedAt >= @cutoff
"""

    assert (
        insert_ctas(sql, "dbo.RecentUsers")
        == """DECLARE @cutoff date = '2026-01-01'
SET @cutoff = DATEADD(day, -7, @cutoff)
CREATE TABLE dbo.RecentUsers AS
SELECT
    u.Id
FROM dbo.Users AS u
WHERE
    u.CreatedAt >= @cutoff
"""
    )


def test_insert_select_into_handles_declarations_before_final_select_without_semicolon():
    sql = """DECLARE @cutoff date = '2026-01-01'
SET @cutoff = DATEADD(day, -7, @cutoff)
SELECT
    u.Id
FROM dbo.Users AS u
WHERE
    u.CreatedAt >= @cutoff
"""

    assert (
        insert_select_into(sql, "dbo.RecentUsers")
        == """DECLARE @cutoff date = '2026-01-01'
SET @cutoff = DATEADD(day, -7, @cutoff)
SELECT
    u.Id
INTO dbo.RecentUsers
FROM dbo.Users AS u
WHERE
    u.CreatedAt >= @cutoff
"""
    )


def test_insert_select_into_handles_select_without_from():
    assert (
        insert_select_into("SELECT 1 AS One", "dbo.OneRow")
        == "SELECT 1 AS One INTO dbo.OneRow"
    )


def test_mixed_case_keywords_work_across_transformers():
    sql = "sEleCt u.Id FrOm dbo.Users as u wHeRe u.IsActive = 1 oRdEr bY u.Id"

    assert (
        insert_where_one_eq_zero(sql)
        == "sEleCt u.Id FrOm dbo.Users as u wHeRe (u.IsActive = 1) AND 1=0 oRdEr bY u.Id"
    )
    assert (
        insert_ctas(sql, "dbo.MixedCase")
        == "CREATE TABLE dbo.MixedCase AS\nsEleCt u.Id FrOm dbo.Users as u wHeRe u.IsActive = 1 oRdEr bY u.Id"
    )
    assert (
        insert_select_into(sql, "dbo.MixedCase")
        == "sEleCt u.Id INTO dbo.MixedCase FrOm dbo.Users as u wHeRe u.IsActive = 1 oRdEr bY u.Id"
    )


def test_adds_where_to_simple_select():
    assert insert_where_one_eq_zero("SELECT * FROM dbo.Users") == "SELECT * FROM dbo.Users WHERE 1=0"


def test_wraps_existing_where_condition():
    assert (
        insert_where_one_eq_zero("SELECT * FROM dbo.Users WHERE IsActive = 1")
        == "SELECT * FROM dbo.Users WHERE (IsActive = 1) AND 1=0"
    )


def test_inserts_before_order_by():
    assert (
        insert_where_one_eq_zero("SELECT * FROM dbo.Users ORDER BY CreatedAt DESC")
        == "SELECT * FROM dbo.Users WHERE 1=0 ORDER BY CreatedAt DESC"
    )


def test_preserves_group_by_after_existing_where():
    assert (
        insert_where_one_eq_zero("SELECT TeamId, COUNT(*) FROM dbo.Users WHERE IsActive = 1 GROUP BY TeamId")
        == "SELECT TeamId, COUNT(*) FROM dbo.Users WHERE (IsActive = 1) AND 1=0 GROUP BY TeamId"
    )


def test_adds_where_after_join():
    sql = "SELECT u.Id FROM dbo.Users u INNER JOIN dbo.Teams t ON t.Id = u.TeamId"
    assert (
        insert_where_one_eq_zero(sql)
        == "SELECT u.Id FROM dbo.Users u INNER JOIN dbo.Teams t ON t.Id = u.TeamId WHERE 1=0"
    )


def test_transforms_cte_and_outer_select():
    sql = "WITH cte AS (SELECT Id FROM dbo.Users) SELECT * FROM cte"
    assert (
        insert_where_one_eq_zero(sql)
        == "WITH cte AS (SELECT Id FROM dbo.Users WHERE 1=0) SELECT * FROM cte WHERE 1=0"
    )


def test_transforms_subquery_in_from_clause():
    sql = "SELECT * FROM (SELECT Id FROM dbo.Users WHERE IsActive = 1) u"
    assert (
        insert_where_one_eq_zero(sql)
        == "SELECT * FROM (SELECT Id FROM dbo.Users WHERE (IsActive = 1) AND 1=0) u WHERE 1=0"
    )


def test_transforms_subquery_inside_existing_where():
    sql = "SELECT * FROM dbo.Teams WHERE Id IN (SELECT TeamId FROM dbo.Users WHERE IsActive = 1)"
    assert (
        insert_where_one_eq_zero(sql)
        == "SELECT * FROM dbo.Teams WHERE (Id IN (SELECT TeamId FROM dbo.Users WHERE (IsActive = 1) AND 1=0)) AND 1=0"
    )


def test_transforms_each_side_of_union():
    sql = "SELECT Id FROM dbo.Users UNION ALL SELECT Id FROM dbo.ArchivedUsers WHERE DeletedAt IS NOT NULL"
    assert (
        insert_where_one_eq_zero(sql)
        == "SELECT Id FROM dbo.Users WHERE 1=0 UNION ALL SELECT Id FROM dbo.ArchivedUsers WHERE (DeletedAt IS NOT NULL) AND 1=0"
    )


def test_transforms_multiple_statements():
    sql = "SELECT 1; SELECT 2 WHERE 2 = 2;"
    assert insert_where_one_eq_zero(sql) == "SELECT 1 WHERE 1=0; SELECT 2 WHERE (2 = 2) AND 1=0;"


def test_handles_tsql_bracketed_identifiers_and_top():
    sql = "SELECT TOP (10) [User Id] FROM [dbo].[Users] WHERE [Status] = 'A'"
    assert (
        insert_where_one_eq_zero(sql)
        == "SELECT TOP (10) [User Id] FROM [dbo].[Users] WHERE ([Status] = 'A') AND 1=0"
    )


def test_adds_where_to_multiline_select_before_order_by():
    sql = """SELECT
    u.Id,
    u.Name
FROM dbo.Users AS u
LEFT JOIN dbo.Teams AS t
    ON t.Id = u.TeamId
ORDER BY
    u.Name;
"""

    assert (
        insert_where_one_eq_zero(sql)
        == """SELECT
    u.Id,
    u.Name
FROM dbo.Users AS u
LEFT JOIN dbo.Teams AS t
    ON t.Id = u.TeamId WHERE 1=0
ORDER BY
    u.Name;
"""
    )


def test_wraps_existing_multiline_where_before_group_by():
    sql = """SELECT
    t.Id,
    COUNT(*) AS UserCount
FROM dbo.Teams AS t
WHERE
    t.IsActive = 1
    AND t.Region IN ('AU', 'NZ')
GROUP BY
    t.Id;
"""

    assert (
        insert_where_one_eq_zero(sql)
        == """SELECT
    t.Id,
    COUNT(*) AS UserCount
FROM dbo.Teams AS t
WHERE (t.IsActive = 1
    AND t.Region IN ('AU', 'NZ')) AND 1=0
GROUP BY
    t.Id;
"""
    )


def test_transforms_long_script_with_comments_temp_table_and_go():
    sql = """-- Build active user extract.
SELECT
    u.Id,
    u.Email
INTO #ActiveUsers
FROM dbo.Users AS u
WHERE
    u.IsActive = 1;

GO

SELECT
    au.Id,
    p.PlanName
FROM #ActiveUsers AS au
JOIN dbo.Plans AS p
    ON p.UserId = au.Id
ORDER BY
    au.Id;
"""

    assert (
        insert_where_one_eq_zero(sql)
        == """-- Build active user extract.
SELECT
    u.Id,
    u.Email
INTO #ActiveUsers
FROM dbo.Users AS u
WHERE (u.IsActive = 1) AND 1=0;

GO

SELECT
    au.Id,
    p.PlanName
FROM #ActiveUsers AS au
JOIN dbo.Plans AS p
    ON p.UserId = au.Id WHERE 1=0
ORDER BY
    au.Id;
"""
    )


def test_transforms_multiline_cte_and_nested_exists():
    sql = """WITH recent_users AS (
    SELECT
        u.Id,
        u.TeamId
    FROM dbo.Users AS u
    WHERE
        u.CreatedAt >= '2026-01-01'
)
SELECT
    t.Id
FROM dbo.Teams AS t
WHERE
    EXISTS (
        SELECT
            1
        FROM recent_users AS ru
        WHERE
            ru.TeamId = t.Id
    );
"""

    assert (
        insert_where_one_eq_zero(sql)
        == """WITH recent_users AS (
    SELECT
        u.Id,
        u.TeamId
    FROM dbo.Users AS u
    WHERE (u.CreatedAt >= '2026-01-01') AND 1=0
)
SELECT
    t.Id
FROM dbo.Teams AS t
WHERE (EXISTS (
        SELECT
            1
        FROM recent_users AS ru
        WHERE (ru.TeamId = t.Id) AND 1=0
    )) AND 1=0;
"""
    )


def test_treats_go_as_batch_separator_without_semicolons():
    sql = """SELECT *
FROM dbo.Users
GO
SELECT *
FROM dbo.Teams
"""

    assert (
        insert_where_one_eq_zero(sql)
        == """SELECT *
FROM dbo.Users WHERE 1=0
GO
SELECT *
FROM dbo.Teams WHERE 1=0
"""
    )


def test_handles_irrelevant_statements_and_adjacent_selects_without_semicolons():
    sql = """DECLARE @cutoff date
SET @cutoff = '2026-01-01'
PRINT 'this string says SELECT * FROM dbo.Nope'
-- This comment has SELECT * FROM dbo.CommentOnly
SELECT @cutoff AS CutoffDate
SELECT
    u.Id
FROM dbo.Users AS u
WHERE
    u.CreatedAt >= @cutoff
IF EXISTS (
    SELECT
        1
    FROM dbo.Teams AS t
    WHERE
        t.IsActive = 1
)
BEGIN
    INSERT INTO #UserIds (Id)
    SELECT
        u.Id
    FROM dbo.Users AS u
    UPDATE dbo.Users SET Seen = 1
END
"""

    assert (
        insert_where_one_eq_zero(sql)
        == """DECLARE @cutoff date
SET @cutoff = '2026-01-01'
PRINT 'this string says SELECT * FROM dbo.Nope'
-- This comment has SELECT * FROM dbo.CommentOnly
SELECT @cutoff AS CutoffDate WHERE 1=0
SELECT
    u.Id
FROM dbo.Users AS u
WHERE (u.CreatedAt >= @cutoff) AND 1=0
IF EXISTS (
    SELECT
        1
    FROM dbo.Teams AS t
    WHERE (t.IsActive = 1) AND 1=0
)
BEGIN
    INSERT INTO #UserIds (Id)
    SELECT
        u.Id
    FROM dbo.Users AS u WHERE 1=0
    UPDATE dbo.Users SET Seen = 1
END
"""
    )


def test_does_not_treat_tsql_table_hint_with_as_statement_boundary():
    sql = "SELECT * FROM dbo.Users WITH (NOLOCK)"

    assert insert_where_one_eq_zero(sql) == "SELECT * FROM dbo.Users WITH (NOLOCK) WHERE 1=0"
