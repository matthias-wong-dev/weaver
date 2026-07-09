from __future__ import annotations

import textwrap

from weaver_runtime.dbrep.ses.sql_discovery import extract_sql_references


def test_two_part_from_reference() -> None:
    refs = extract_sql_references("select * from Stage.Record")
    assert refs == (("Stage", "Record"),)


def test_three_part_cross_database_reference() -> None:
    refs = extract_sql_references("select * from T0.Raw.Drop")
    assert refs == (("T0", "Raw", "Drop"),)


def test_four_part_reference_is_captured() -> None:
    refs = extract_sql_references("select * from Server.T0.Raw.Drop")
    assert refs == (("Server", "T0", "Raw", "Drop"),)


def test_joins_and_from_lists() -> None:
    sql = textwrap.dedent(
        """
        select o.OrderNumber, c.CustomerCode
        from raw.Order as o
        join dim.Customer as c on c.CustomerCode = o.CustomerCode
        left join T0.ref.Fx as fx on fx.RateDate = o.OrderDate, mart.Extra e
        where o.OrderAmount > 0
        """
    )
    refs = set(extract_sql_references(sql))
    assert ("raw", "Order") in refs
    assert ("dim", "Customer") in refs
    assert ("T0", "ref", "Fx") in refs
    assert ("mart", "Extra") in refs


def test_ignores_single_part_ctes_and_aliases() -> None:
    sql = textwrap.dedent(
        """
        with recent as (
            select * from Stage.Record
        )
        select * from recent r
        join Mart.Summary s on s.id = r.id
        """
    )
    refs = set(extract_sql_references(sql))
    assert ("Stage", "Record") in refs
    assert ("Mart", "Summary") in refs
    # 'recent' is a single-part CTE reference and must not be captured.
    assert all(len(parts) >= 2 for parts in refs)
    assert not any(parts[-1] == "recent" for parts in refs)


def test_ignores_bracketed_identifiers_delimiters() -> None:
    refs = extract_sql_references("select * from [Stage].[Record]")
    assert refs == (("Stage", "Record"),)


def test_where_clause_columns_are_not_relations() -> None:
    sql = "select * from Stage.Record where Some.Column = 1"
    refs = extract_sql_references(sql)
    assert refs == (("Stage", "Record"),)
