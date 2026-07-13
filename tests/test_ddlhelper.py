from pathlib import Path

import pytest

from source.ddlhelper import (
    build_create_table_sql_from_describe_rows,
    generate_infer_create_table_sql,
    generate_ses_repository_ddl,
    generate_ses_repository_ddl_sql,
    wrap_create_or_alter_view,
)
from source.etlhelper import generate_load_stored_procedure_sql
from source.seshelper import SesSyntaxException, parse_ses_sql, read_ses_sql_file


FABRIC_SAMPLE_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "fabric_sample_sql"
SES_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "ses"
SES_DAG_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "ses_dag"


def _fabric_sample_fixture_sql(name):
    return (FABRIC_SAMPLE_FIXTURE_DIR / name).read_text()


def _ses_fixture(name):
    return read_ses_sql_file(SES_FIXTURE_DIR / name)


def _sql_with_metadata(metadata: str, body: str) -> str:
    return f"/*\n{metadata.strip()}\n*/\n\n{body}"


def _layer_contains(layer, marker):
    return any(marker in sql for sql in layer)


def _materialise_installed_procedure(installer_sql, replacements):
    marker = "set @weaver_proc_sql = N'"
    start = installer_sql.index(marker) + len(marker)
    end = installer_sql.index("';\nset @weaver_proc_sql = replace", start)
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
    shape_sql = transformed.split("declare @weaver_identity_column", maxsplit=1)[0]

    assert "into #weaver_shape_test" in shape_sql
    assert "1=0" in shape_sql
    assert "create table [dbo].[generated_from_sys_Current]" in transformed
    assert "create table [dbo].[generated_from_sys_History]" in transformed
    assert "create or alter view [dbo].[generated_from_sys]" in transformed
    assert "from tempdb.sys.columns as c" in transformed
    assert "inner join tempdb.sys.types as t" in transformed
    assert "object_id(N'tempdb..#weaver_shape_test')" in transformed
    assert "sys.dm_exec_describe_first_result_set" not in transformed
    assert "quotename(@weaver_identity_column) + N' bigint identity not null'" in transformed
    assert "N'[Row delete datetime] datetime2(6) not null'" in transformed
    assert "(1, N'GeneratedSK')" in transformed
    assert "+ N'primary key nonclustered ('" in transformed
    assert "+ N') not enforced;'" in transformed
    assert "exec sys.sp_executesql @weaver_current_create_sql;" in transformed
    assert "exec sys.sp_executesql @weaver_history_create_sql;" in transformed
    assert "exec sys.sp_executesql @weaver_current_pk_sql;" in transformed
    assert "exec sys.sp_executesql @weaver_view_sql;" in transformed


def test_generate_infer_create_table_sql_accepts_ses_document_metadata():
    document = _ses_fixture("mart.Customer.sql")

    transformed = generate_infer_create_table_sql(
        document,
        temp_table_name="#weaver_shape_ses_customer",
    )

    assert "into #weaver_shape_ses_customer" in transformed
    assert "create table [mart].[Customer_Current]" in transformed
    assert "create table [mart].[Customer_History]" in transformed
    assert "create or alter view [mart].[Customer]" in transformed
    assert "declare @weaver_identity_column varchar(128) = N'Customer SK';" in transformed
    assert "(1, N'CustomerCode')" in transformed
    assert "throw 51004, @weaver_missing_metadata_column, 1;" in transformed
    assert "N'Primary key' as metadata_kind" in transformed
    assert "N'Unique key' as metadata_kind" in transformed
    assert "N'Column notes' as metadata_kind" in transformed
    assert "concat(metadata_kind, N' ', column_name, N' does not exist')" in transformed


def test_generate_infer_create_table_sql_emits_remote_metadata_column_errors():
    document = parse_ses_sql(
        """/*
Table ID: mart.Customer
Description: Customer table.
Primary key: MissingPK
Unique keys:
    - MissingUQ
Revisions:
    - 2026-06-16 Initial table.
Column notes:
    MissingNote: This column is not in the query.
*/\nselect CustomerCode from dbo.SourceCustomers"""
    )

    transformed = generate_infer_create_table_sql(
        document,
        temp_table_name="#weaver_shape_ses_missing_columns",
    )

    assert "N'Primary key' as metadata_kind\n      , N'MissingPK' as column_name" in transformed
    assert "N'Unique key' as metadata_kind\n      , N'MissingUQ' as column_name" in transformed
    assert "N'Column notes' as metadata_kind\n      , N'MissingNote' as column_name" in transformed
    assert "concat(metadata_kind, N' ', column_name, N' does not exist')" in transformed
    assert "throw 51004, @weaver_missing_metadata_column, 1;" in transformed


def test_generate_infer_create_table_sql_contains_yaml_type_mappings():
    transformed = generate_infer_create_table_sql(
        "SELECT cast(N'x' as nvarchar(50)) as Name",
        "report.MixedTypes",
        temp_table_name="#weaver_shape_types",
    )

    assert "when 'nvarchar' then N'varchar('" in transformed
    assert "when 'nchar' then N'varchar('" in transformed
    assert "when 'tinyint' then N'smallint'" in transformed
    assert "when 'money' then N'decimal(' + N'19' + N',' + N'4' + N')'" in transformed
    assert "when 'datetime' then N'datetime2(' + N'6' + N')'" in transformed
    assert "when 'varbinary' then N'varbinary('" in transformed


def test_generate_infer_create_table_sql_quotes_target_and_identity_names():
    transformed = generate_infer_create_table_sql(
        "SELECT 1 as Id",
        "audit.[Odd.Table Name]",
        identity_column="Table SK",
        temp_table_name="#weaver_shape_quote",
    )

    assert "create table [audit].[Odd.Table Name_Current]" in transformed
    assert "create table [audit].[Odd.Table Name_History]" in transformed
    assert "create or alter view [audit].[Odd.Table Name]" in transformed
    assert "declare @weaver_identity_column varchar(128) = N'Table SK';" in transformed
    assert "SELECT 1 as Id into #weaver_shape_quote where 1=0" in transformed


def test_generate_infer_create_table_sql_without_identity_uses_null_identity():
    transformed = generate_infer_create_table_sql(
        "SELECT 1 as Id",
        "dbo.NoIdentity",
        temp_table_name="#weaver_shape_no_identity",
    )

    assert "declare @weaver_identity_column varchar(128) = null;" in transformed
    assert "where @weaver_identity_column is not null" in transformed


def test_generate_infer_create_table_sql_disambiguates_duplicate_column_names():
    transformed = generate_infer_create_table_sql(
        "SELECT 1 as SameName, 2 as SameName, 3",
        "dbo.DuplicateColumns",
        temp_table_name="#weaver_shape_duplicates",
    )

    assert "coalesce(nullif(c.name, ''), concat('Column', c.column_id))" in transformed
    assert "count(*) over (partition by column_name)" in transformed
    assert "row_number() over (partition by column_name order by column_ordinal)" in transformed


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
        "create table [dbo].[Inferred_Current] (\n"
        "    [InferredSK] bigint identity not null\n"
        "  , [Name] varchar(128) null\n"
        "  , [Amount] decimal(19,4) not null\n"
        "  , [Column3] smallint null\n"
        "  , [Row insert datetime] datetime2(6) null\n"
        "  , [Row update datetime] datetime2(6) null\n"
        "  , [Row delete datetime] datetime2(6) not null\n"
        ");\n\n"
        "create table [dbo].[Inferred_History] (\n"
        "    [InferredSK] bigint not null\n"
        "  , [Name] varchar(128) null\n"
        "  , [Amount] decimal(19,4) not null\n"
        "  , [Column3] smallint null\n"
        "  , [Row insert datetime] datetime2(6) null\n"
        "  , [Row update datetime] datetime2(6) null\n"
        "  , [Row delete datetime] datetime2(6) not null\n"
        ");\n\n"
        "alter table [dbo].[Inferred_Current] add constraint [PK_Inferred_Current] "
        "primary key nonclustered ([InferredSK]) not enforced;\n\n"
        "create or alter view [dbo].[Inferred] as\n"
        "select\n"
        "    [InferredSK]\n"
        "  , [Name]\n"
        "  , [Amount]\n"
        "  , [Column3]\n"
        "  , [Row insert datetime]\n"
        "  , [Row update datetime]\n"
        "from [dbo].[Inferred_Current];"
    )


def test_build_create_table_sql_from_describe_rows_accepts_ses_metadata():
    metadata = _ses_fixture("mart.Customer.sql").metadata
    rows = [
        {
            "is_hidden": False,
            "column_ordinal": 1,
            "name": "CustomerCode",
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
            "name": "CustomerSegment",
            "is_nullable": True,
            "system_type_name": "varchar(40)",
            "max_length": 40,
            "precision": 0,
            "scale": 0,
            "error_number": None,
        },
    ]

    create_sql = build_create_table_sql_from_describe_rows(rows, metadata)

    assert "create table [mart].[Customer_Current]" in create_sql
    assert "[Customer SK] bigint identity not null" in create_sql
    assert "[CustomerCode] varchar(20) not null" in create_sql
    assert (
        "alter table [mart].[Customer_Current] add constraint [PK_Customer_Current] "
        "primary key nonclustered ([CustomerCode]) not enforced;"
    ) in create_sql


def test_build_create_table_sql_from_describe_rows_validates_ses_metadata_columns():
    metadata = parse_ses_sql(
        """/*
Table ID: mart.Customer
Description: Customer table.
Primary key: MissingKey
Revisions:
    - 2026-06-16 Initial table.
*/\nselect CustomerCode from dbo.SourceCustomers"""
    ).metadata

    with pytest.raises(ValueError, match="Primary key MissingKey does not exist"):
        build_create_table_sql_from_describe_rows(
            [
                {
                    "is_hidden": False,
                    "column_ordinal": 1,
                    "name": "CustomerCode",
                    "is_nullable": True,
                    "system_type_name": "varchar(20)",
                    "max_length": 20,
                    "precision": 0,
                    "scale": 0,
                    "error_number": None,
                },
            ],
            metadata,
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

    assert "[Duplicate_1] int null" in create_sql
    assert "[Duplicate_2] int null" in create_sql


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

    current_section = create_sql.split("create table [warehouse].[Customer_History]", maxsplit=1)[0]
    history_section = create_sql.split("create table [warehouse].[Customer_History]", maxsplit=1)[1].split("alter table", maxsplit=1)[0]
    view_section = create_sql.split("create or alter view [warehouse].[Customer] as", maxsplit=1)[1]

    assert "[BusinessKey] varchar(20) not null" in current_section
    assert "alter table [warehouse].[Customer_Current] add constraint [PK_Customer_Current] primary key nonclustered ([BusinessKey]) not enforced;" in create_sql
    assert "primary key" not in history_section.split("create or alter view", maxsplit=1)[0]
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


def test_generate_ses_repository_ddl_sql_layers_objects_then_load_procedures():
    ddl_layers = generate_ses_repository_ddl_sql(SES_DAG_FIXTURE_DIR)

    assert [len(layer) for layer in ddl_layers] == [2, 2, 1, 1, 1, 5]

    assert _layer_contains(ddl_layers[0], "create table [dim].[Product_Current]")
    assert _layer_contains(ddl_layers[0], "create table [raw].[Customer_Current]")
    assert _layer_contains(ddl_layers[1], "create table [dim].[Customer_Current]")
    assert _layer_contains(ddl_layers[1], "create table [raw].[Order_Current]")
    assert _layer_contains(ddl_layers[2], "create table [fact].[Order_Current]")
    assert _layer_contains(
        ddl_layers[3],
        "create or alter view [report].[CustomerOrderSummary] as",
    )
    assert _layer_contains(
        ddl_layers[4],
        "create or alter view [audit].[CustomerOrderQuality] as",
    )

    procedure_layer = ddl_layers[-1]
    assert all(
        sql.startswith("/* weaver generated etl procedure installer. */")
        for sql in procedure_layer
    )
    assert _layer_contains(
        procedure_layer,
        "set @weaver_proc_sql = N'create or alter procedure [_].[ETL dim.Customer]",
    )
    assert _layer_contains(
        procedure_layer,
        "set @weaver_proc_sql = N'create or alter procedure [_].[ETL dim.Product]",
    )
    assert _layer_contains(
        procedure_layer,
        "set @weaver_proc_sql = N'create or alter procedure [_].[ETL fact.Order]",
    )
    assert _layer_contains(
        procedure_layer,
        "set @weaver_proc_sql = N'create or alter procedure [_].[ETL raw.Customer]",
    )
    assert _layer_contains(
        procedure_layer,
        "set @weaver_proc_sql = N'create or alter procedure [_].[ETL raw.Order]",
    )


def test_generate_ses_repository_ddl_uses_stable_temp_table_names():
    ddl_layers = generate_ses_repository_ddl(
        SES_DAG_FIXTURE_DIR,
        temp_table_name_prefix="#shape",
    )

    assert _layer_contains(ddl_layers[0], "into #shape_dim_Product")
    assert _layer_contains(ddl_layers[0], "into #shape_raw_Customer")
    assert _layer_contains(ddl_layers[1], "into #shape_dim_Customer")
    assert _layer_contains(ddl_layers[1], "into #shape_raw_Order")


def test_generate_ses_repository_ddl_sql_rejects_dependency_cycles(tmp_path):
    (tmp_path / "a.A.sql").write_text(
        _sql_with_metadata(
            """
View ID: a.A
Description: Cycle A.
Revisions:
    - 2026-06-16 Initial.
""",
            "select * from b.B;\n",
        ),
        encoding="utf-8",
    )
    (tmp_path / "b.B.sql").write_text(
        _sql_with_metadata(
            """
View ID: b.B
Description: Cycle B.
Revisions:
    - 2026-06-16 Initial.
""",
            "select * from a.A;\n",
        ),
        encoding="utf-8",
    )

    with pytest.raises(SesSyntaxException, match="dependency cycle"):
        generate_ses_repository_ddl_sql(tmp_path)


def test_generate_load_stored_procedure_sql_builds_pk_complete_loader():
    installer_sql = generate_load_stored_procedure_sql(
        "select CustomerCode, CustomerName, Balance from dbo.SourceCustomers",
        "mart.Customer",
        primary_key_columns=["CustomerCode"],
    )
    installer_prefix = installer_sql.split("set @weaver_proc_sql", maxsplit=1)[0]

    assert installer_sql.startswith("/* weaver generated etl procedure installer. */")
    assert "object_id(N'[mart].[Customer_Current]')" in installer_prefix
    assert "create table [mart].[Customer_Staging] as" not in installer_prefix
    assert "set @weaver_proc_sql = N'create or alter procedure [_].[ETL mart.Customer]" in installer_sql
    assert "create table [mart].[Customer_Staging] as" in installer_sql
    assert "create table [mart].[Customer_Accepted] as" in installer_sql
    assert "create table [mart].[Customer_Upsert] as" in installer_sql
    assert "create table [mart].[Customer_Reject] as" in installer_sql
    assert "left join [mart].[Customer] as t on s.[CustomerCode] = t.[CustomerCode]" in installer_sql
    assert "or exists (" in installer_sql
    assert "except" in installer_sql
    assert "as [_Is new row]" in installer_sql
    assert "cast(''null primary key'' as varchar(100))" in installer_sql
    assert "cast(''duplicate primary key'' as varchar(100))" in installer_sql
    assert "row_number() over (" in installer_sql
    assert "partition by s.[CustomerCode]" in installer_sql
    assert "from [mart].[Customer_Accepted] as s" in installer_sql
    assert "insert into [mart].[Customer_Current]" in installer_sql
    assert "insert into [mart].[Customer_History]" in installer_sql
    assert "update c" in installer_sql
    assert "delete c" in installer_sql
    assert "where u.[_Is new row] = 1" in installer_sql
    assert "where u.[_Is new row] = 0" in installer_sql
    assert "c.[Row update datetime] = @weaver_load_datetime" in installer_sql
    assert "when name = N'Row delete datetime' and row_ordinal = 1 then N'@weaver_load_datetime as ' + quotename(name)" in installer_sql
    assert "delete h" in installer_sql
    assert "if not exists (\n        select 1\n        from [mart].[Customer_Reject]\n    )" in installer_sql
    assert "set @weaver_proc_sql = replace(@weaver_proc_sql, N'__SOURCE_COLUMNS__', @weaver_source_columns);" in installer_sql

    assert _materialise_installed_procedure(
        installer_sql,
        {
            "__SOURCE_COLUMNS__": "[CustomerCode]\n      , [CustomerName]\n      , [Balance]",
            "__STAGING_SELECT_COLUMNS__": "s.[CustomerCode]\n      , s.[CustomerName]\n      , s.[Balance]",
            "__STAGING_EXCEPT_COLUMNS__": "s.[CustomerCode]\n                  , s.[CustomerName]\n                  , s.[Balance]",
            "__UPSERT_SELECT_COLUMNS__": "u.[CustomerCode]\n      , u.[CustomerName]\n      , u.[Balance]",
            "__TARGET_SELECT_COLUMNS__": "t.[CustomerCode]\n      , t.[CustomerName]\n      , t.[Balance]",
            "__TARGET_EXCEPT_COLUMNS__": "t.[CustomerCode]\n                  , t.[CustomerName]\n                  , t.[Balance]",
            "__HISTORY_COLUMNS__": "[CustomerCode]\n          , [CustomerName]\n          , [Balance]\n          , [Row insert datetime]\n          , [Row update datetime]\n          , [Row delete datetime]",
            "__HISTORY_SELECT_COLUMNS__": "c.[CustomerCode]\n          , c.[CustomerName]\n          , c.[Balance]\n          , c.[Row insert datetime]\n          , c.[Row update datetime]\n          , @weaver_load_datetime as [Row delete datetime]",
            "__UPDATE_SET_COLUMNS__": "c.[CustomerName] = u.[CustomerName]\n          , c.[Balance] = u.[Balance]\n          , c.[Row update datetime] = @weaver_load_datetime\n          , c.[Row delete datetime] = convert(datetime2(6), '9999-12-31 00:00:00')",
        },
    ) == """create or alter procedure [_].[ETL mart.Customer]
as
begin
    set nocount on;
    declare @weaver_load_datetime datetime2(6) = sysutcdatetime();

    if object_id(N'[mart].[Customer_Reject]', N'U') is not null drop table [mart].[Customer_Reject];
    if object_id(N'[mart].[Customer_Upsert]', N'U') is not null drop table [mart].[Customer_Upsert];
    if object_id(N'[mart].[Customer_Accepted]', N'U') is not null drop table [mart].[Customer_Accepted];
    if object_id(N'[mart].[Customer_Staging]', N'U') is not null drop table [mart].[Customer_Staging];

    create table [mart].[Customer_Staging] as
    select CustomerCode, CustomerName, Balance from dbo.SourceCustomers;

    create table [mart].[Customer_Accepted] as
    select
        s.*
      , row_number() over (
            partition by s.[CustomerCode]
            order by (select 0)
        ) as [__weaver_pk_row_number]
    from [mart].[Customer_Staging] as s;

    create table [mart].[Customer_Reject] as
    select
        s.[CustomerCode]
      , s.[CustomerName]
      , s.[Balance]
      , case
            when nullif(trim(cast(s.[CustomerCode] as varchar(max))), '') is null then cast('null primary key' as varchar(100))
            when s.[__weaver_pk_row_number] > 1 then cast('duplicate primary key' as varchar(100))
        end as [Rejection reason]
    from [mart].[Customer_Accepted] as s
    where
        (
            nullif(trim(cast(s.[CustomerCode] as varchar(max))), '') is null
            or s.[__weaver_pk_row_number] > 1
        );

    delete s
    from [mart].[Customer_Accepted] as s
    where
        (
            nullif(trim(cast(s.[CustomerCode] as varchar(max))), '') is null
            or s.[__weaver_pk_row_number] > 1
        );

    create table [mart].[Customer_Upsert] as
    select
        s.[CustomerCode]
      , s.[CustomerName]
      , s.[Balance]
      , case when t.[CustomerCode] is null then cast(1 as int) else cast(0 as int) end as [_Is new row]
    from [mart].[Customer_Accepted] as s
    left join [mart].[Customer] as t on s.[CustomerCode] = t.[CustomerCode]
    where
        (
            t.[CustomerCode] is null
            or exists (
                select
                    s.[CustomerCode]
                  , s.[CustomerName]
                  , s.[Balance]
                except
                select
                    t.[CustomerCode]
                  , t.[CustomerName]
                  , t.[Balance]
            )
        );

    insert into [mart].[Customer_Current] (
        [CustomerCode]
      , [CustomerName]
      , [Balance]
      , [Row insert datetime]
      , [Row update datetime]
      , [Row delete datetime]
    )
    select
        u.[CustomerCode]
      , u.[CustomerName]
      , u.[Balance]
      , @weaver_load_datetime
      , @weaver_load_datetime
      , convert(datetime2(6), '9999-12-31 00:00:00')
    from [mart].[Customer_Upsert] as u
    where u.[_Is new row] = 1;

    begin try
        insert into [mart].[Customer_History] (
            [CustomerCode]
          , [CustomerName]
          , [Balance]
          , [Row insert datetime]
          , [Row update datetime]
          , [Row delete datetime]
        )
        select
            c.[CustomerCode]
          , c.[CustomerName]
          , c.[Balance]
          , c.[Row insert datetime]
          , c.[Row update datetime]
          , @weaver_load_datetime as [Row delete datetime]
        from [mart].[Customer_Current] as c
        inner join [mart].[Customer_Upsert] as u on c.[CustomerCode] = u.[CustomerCode]
        where u.[_Is new row] = 0;

        update c
        set
            c.[CustomerName] = u.[CustomerName]
          , c.[Balance] = u.[Balance]
          , c.[Row update datetime] = @weaver_load_datetime
          , c.[Row delete datetime] = convert(datetime2(6), '9999-12-31 00:00:00')
        from [mart].[Customer_Current] as c
        inner join [mart].[Customer_Upsert] as u on c.[CustomerCode] = u.[CustomerCode]
        where u.[_Is new row] = 0;
    end try
    begin catch
        delete h
        from [mart].[Customer_History] as h
        inner join [mart].[Customer_Upsert] as u on h.[CustomerCode] = u.[CustomerCode]
        where u.[_Is new row] = 0
            and h.[Row delete datetime] = @weaver_load_datetime;

        throw;
    end catch;

    begin try
        insert into [mart].[Customer_History] (
            [CustomerCode]
          , [CustomerName]
          , [Balance]
          , [Row insert datetime]
          , [Row update datetime]
          , [Row delete datetime]
        )
        select
            c.[CustomerCode]
          , c.[CustomerName]
          , c.[Balance]
          , c.[Row insert datetime]
          , c.[Row update datetime]
          , @weaver_load_datetime as [Row delete datetime]
        from [mart].[Customer_Current] as c
        where not exists (select 1 from [mart].[Customer_Accepted] as s where s.[CustomerCode] = c.[CustomerCode]);

        delete c
        from [mart].[Customer_Current] as c
        where not exists (select 1 from [mart].[Customer_Accepted] as s where s.[CustomerCode] = c.[CustomerCode]);
    end try
    begin catch
        delete h
        from [mart].[Customer_History] as h
        where h.[Row delete datetime] = @weaver_load_datetime
            and not exists (select 1 from [mart].[Customer_Accepted] as s where s.[CustomerCode] = h.[CustomerCode]);

        throw;
    end catch;

    if not exists (
        select 1
        from [mart].[Customer_Reject]
    )
    begin
        if object_id(N'[mart].[Customer_Reject]', N'U') is not null drop table [mart].[Customer_Reject];
        if object_id(N'[mart].[Customer_Upsert]', N'U') is not null drop table [mart].[Customer_Upsert];
        if object_id(N'[mart].[Customer_Accepted]', N'U') is not null drop table [mart].[Customer_Accepted];
        if object_id(N'[mart].[Customer_Staging]', N'U') is not null drop table [mart].[Customer_Staging];
    end;
end;"""


def test_generate_load_stored_procedure_sql_rejects_null_primary_keys():
    installer_sql = generate_load_stored_procedure_sql(
        "select CustomerCode, CustomerName, Balance from dbo.SourceCustomers",
        "mart.Customer",
        primary_key_columns=["CustomerCode"],
    )

    assert "create table [mart].[Customer_Reject] as" in installer_sql
    assert "when nullif(trim(cast(s.[CustomerCode] as varchar(max))), '''') is null then cast(''null primary key'' as varchar(100))" in installer_sql
    assert (
        "delete s\n"
        "    from [mart].[Customer_Accepted] as s\n"
        "    where\n"
        "        (\n"
        "            nullif(trim(cast(s.[CustomerCode] as varchar(max))), '''') is null"
    ) in installer_sql


def test_generate_load_stored_procedure_sql_rejects_duplicate_primary_keys():
    installer_sql = generate_load_stored_procedure_sql(
        "select CustomerCode, CustomerName, Balance from dbo.SourceCustomers",
        "mart.Customer",
        primary_key_columns=["CustomerCode"],
    )

    assert "partition by s.[CustomerCode]" in installer_sql
    assert "as [__weaver_pk_row_number]" in installer_sql
    assert "when s.[__weaver_pk_row_number] > 1 then cast(''duplicate primary key'' as varchar(100))" in installer_sql
    assert "or s.[__weaver_pk_row_number] > 1" in installer_sql
    assert "delete s\n    from [mart].[Customer_Accepted] as s" in installer_sql


def test_generate_incremental_loader_omits_missing_key_reconciliation():
    installer_sql = generate_load_stored_procedure_sql(
        "select CustomerCode, CustomerName from dbo.SourceCustomers",
        "mart.Customer",
        primary_key_columns=["CustomerCode"],
        is_incremental=True,
    )

    assert "create table [mart].[Customer_Accepted] as" in installer_sql
    assert "create table [mart].[Customer_Upsert] as" in installer_sql
    assert "not exists (select 1 from [mart].[Customer_Accepted]" not in installer_sql


def test_generate_load_stored_procedure_sql_keeps_artifacts_when_reject_is_not_empty():
    installer_sql = generate_load_stored_procedure_sql(
        "select CustomerCode, CustomerName, Balance from dbo.SourceCustomers",
        "mart.Customer",
        primary_key_columns=["CustomerCode"],
    )

    assert (
        "if not exists (\n"
        "        select 1\n"
        "        from [mart].[Customer_Reject]\n"
        "    )\n"
        "    begin\n"
        "        if object_id(N''[mart].[Customer_Reject]'', N''U'') is not null drop table [mart].[Customer_Reject];\n"
        "        if object_id(N''[mart].[Customer_Upsert]'', N''U'') is not null drop table [mart].[Customer_Upsert];\n"
        "        if object_id(N''[mart].[Customer_Accepted]'', N''U'') is not null drop table [mart].[Customer_Accepted];\n"
        "        if object_id(N''[mart].[Customer_Staging]'', N''U'') is not null drop table [mart].[Customer_Staging];\n"
        "    end;"
    ) in installer_sql


def test_generate_load_stored_procedure_sql_builds_full_refresh_without_pk():
    installer_sql = generate_load_stored_procedure_sql(
        "select ProductCode, ProductName from dbo.SourceProducts;",
        "mart.Product",
    )

    assert "set @weaver_proc_sql = N'create or alter procedure [_].[ETL mart.Product]" in installer_sql
    assert "object_id(N'[mart].[Product_Current]')" in installer_sql
    assert "create table [mart].[Product_Staging] as" in installer_sql
    assert "create table [mart].[Product_Upsert] as" not in installer_sql
    assert "delete from [mart].[Product_Current];" in installer_sql
    assert "insert into [mart].[Product_Current]" in installer_sql
    assert "insert into [mart].[Product_History]" not in installer_sql
    assert "[Row insert datetime]" in installer_sql
    assert "[Row update datetime]" in installer_sql
    assert "[Row delete datetime]" in installer_sql

    assert _materialise_installed_procedure(
        installer_sql,
        {
            "__SOURCE_COLUMNS__": "[ProductCode]\n      , [ProductName]",
            "__STAGING_SELECT_COLUMNS__": "s.[ProductCode]\n      , s.[ProductName]",
            "__STAGING_EXCEPT_COLUMNS__": "",
            "__UPSERT_SELECT_COLUMNS__": "",
            "__TARGET_SELECT_COLUMNS__": "",
            "__TARGET_EXCEPT_COLUMNS__": "",
            "__HISTORY_COLUMNS__": "[ProductCode]\n          , [ProductName]\n          , [Row insert datetime]\n          , [Row update datetime]\n          , [Row delete datetime]",
            "__HISTORY_SELECT_COLUMNS__": "",
            "__UPDATE_SET_COLUMNS__": "",
        },
    ) == """create or alter procedure [_].[ETL mart.Product]
as
begin
    set nocount on;
    declare @weaver_load_datetime datetime2(6) = sysutcdatetime();

    if object_id(N'[mart].[Product_Reject]', N'U') is not null drop table [mart].[Product_Reject];
    if object_id(N'[mart].[Product_Upsert]', N'U') is not null drop table [mart].[Product_Upsert];
    if object_id(N'[mart].[Product_Accepted]', N'U') is not null drop table [mart].[Product_Accepted];
    if object_id(N'[mart].[Product_Staging]', N'U') is not null drop table [mart].[Product_Staging];

    create table [mart].[Product_Staging] as
    select ProductCode, ProductName from dbo.SourceProducts;

    delete from [mart].[Product_Current];

    insert into [mart].[Product_Current] (
        [ProductCode]
      , [ProductName]
      , [Row insert datetime]
      , [Row update datetime]
      , [Row delete datetime]
    )
    select
        s.[ProductCode]
      , s.[ProductName]
      , @weaver_load_datetime
      , @weaver_load_datetime
      , convert(datetime2(6), '9999-12-31 00:00:00')
    from [mart].[Product_Staging] as s;

    if object_id(N'[mart].[Product_Reject]', N'U') is not null drop table [mart].[Product_Reject];
    if object_id(N'[mart].[Product_Upsert]', N'U') is not null drop table [mart].[Product_Upsert];
    if object_id(N'[mart].[Product_Accepted]', N'U') is not null drop table [mart].[Product_Accepted];
    if object_id(N'[mart].[Product_Staging]', N'U') is not null drop table [mart].[Product_Staging];
end;"""


def test_generate_load_stored_procedure_sql_quotes_names_and_composite_pk():
    installer_sql = generate_load_stored_procedure_sql(
        "select [Order Id], [Line No], Amount from dbo.SourceOrderLines",
        "audit.[Order Line]",
        primary_key_columns=["Order Id", "Line No"],
    )

    assert "create or alter procedure [_].[ETL audit.Order Line]" in installer_sql
    assert "create table [audit].[Order Line_Staging] as" in installer_sql
    assert (
        "left join [audit].[Order Line] as t on "
        "s.[Order Id] = t.[Order Id]\n        and s.[Line No] = t.[Line No]"
    ) in installer_sql
    assert "create table [audit].[Order Line_Reject] as" in installer_sql
    assert "partition by s.[Order Id], s.[Line No]" in installer_sql
    assert (
        "when (\n"
        "                    nullif(trim(cast(s.[Order Id] as varchar(max))), '''') is null\n"
        "                    or nullif(trim(cast(s.[Line No] as varchar(max))), '''') is null\n"
        "                ) then cast(''null primary key'' as varchar(100))"
    ) in installer_sql
    assert (
        "from [audit].[Order Line_Accepted] as s where "
        "s.[Order Id] = c.[Order Id]\n        and s.[Line No] = c.[Line No]"
    ) in installer_sql
    assert "N'order id'" in installer_sql
    assert "N'line no'" in installer_sql


def test_wrap_create_or_alter_view_for_plain_select():
    assert wrap_create_or_alter_view(
        "SELECT customer_id from dbo.weaver_fixture_customers;",
        "report.CustomerView",
    ) == (
        "create or alter view [report].[CustomerView] as\n"
        "SELECT customer_id from dbo.weaver_fixture_customers"
    )


def test_wrap_create_or_alter_view_for_cte():
    sql = """WITH active_customers as (
    select
        customer_id
    from dbo.weaver_fixture_customers
    where is_active = 1
)
select
    customer_id
FROM active_customers;
"""

    assert wrap_create_or_alter_view(sql, "dbo.ActiveCustomers") == """create or alter view [dbo].[ActiveCustomers] as
WITH active_customers as (
    select
        customer_id
    from dbo.weaver_fixture_customers
    where is_active = 1
)
select
    customer_id
FROM active_customers"""


def test_wrap_create_or_alter_view_strips_leading_semicolon_before_cte():
    assert wrap_create_or_alter_view(
        ";WITH cte as (SELECT 1 as id) select id from cte;",
        "[dbo].[CteView]",
    ) == (
        "create or alter view [dbo].[CteView] as\n"
        "WITH cte as (SELECT 1 as id) select id from cte"
    )


def test_wrap_create_or_alter_view_quotes_odd_view_name():
    assert wrap_create_or_alter_view(
        "SELECT 1 as id",
        "report.[Odd.View Name]",
    ).startswith("create or alter view [report].[Odd.View Name] as")
