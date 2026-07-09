from __future__ import annotations

import urllib.request
from pathlib import Path

import pytest

from dbrep_helpers import write_config_files
from weaver_runtime.dbrep.cli.commands import run_wipe
from weaver_runtime.dbrep.sql.connection import connect, execute_script, query

pytestmark = pytest.mark.fabric


# --- SQL warehouse wipe ----------------------------------------------------


def _create_rich_objects(server: str, database: str) -> None:
    statements = [
        "if schema_id('Ref') is null exec('create schema Ref')",
        "if schema_id('Fact') is null exec('create schema Fact')",
        "if schema_id('Report') is null exec('create schema Report')",
        "create table Ref.Thing (id int not null, name varchar(50) null)",
        "alter table Ref.Thing add constraint PK_Ref_Thing primary key nonclustered (id) not enforced",
        "create table Fact.Line (line_id int not null, thing_id int null)",
        "alter table Fact.Line add constraint PK_Fact_Line primary key nonclustered (line_id) not enforced",
        "alter table Fact.Line add constraint FK_Fact_Line_Thing "
        "foreign key (thing_id) references Ref.Thing(id) not enforced",
        "create view Report.Joined as "
        "select l.line_id, t.name from Fact.Line l left join Ref.Thing t on t.id = l.thing_id",
        "create procedure Ref.DoNothing as begin select 1 as ok; end",
    ]
    with connect(server, database) as conn:
        for statement in statements:
            execute_script(conn, statement)
        # Scalar function is best-effort (T-SQL surface varies).
        try:
            execute_script(
                conn,
                "create function Ref.AddOne(@x int) returns int as begin return (@x + 1); end",
            )
        except Exception:
            pass


def _user_object_count(server: str, database: str) -> int:
    with connect(server, database) as conn:
        return query(
            conn,
            "select count(*) as n from sys.objects "
            "where lower(schema_name(schema_id)) not in "
            "(N'sys', N'information_schema', N'queryinsights', N'dbo') "
            "and type in (N'U', N'V', N'P', N'FN', N'IF', N'TF')",
        )[0]["n"]


def test_sql_wipe_clears_all_objects(tmp_path: Path, fabric_sql_target) -> None:
    server, database = fabric_sql_target["server"], fabric_sql_target["database"]
    _create_rich_objects(server, database)
    assert _user_object_count(server, database) >= 4  # 2 tables + view + proc

    weaver = write_config_files(
        tmp_path,
        {"Warehouse": {"server": server, "degrees_of_parallelism": fabric_sql_target["dop"]}},
        {"WH": {"type": "SQL", "server": "Warehouse", "database": database}},
    )
    result = run_wipe(weaver, "WH")
    assert result["type"] == "SQL"
    assert result["after"] == {}
    assert _user_object_count(server, database) == 0


# --- Lakehouse (Files / Delta) wipe ----------------------------------------


def _dfs_put(info: dict, relative: str, content: bytes) -> None:
    base = info["onelake_base_url"].rstrip("/")
    url = f"{base}/{info['workspace_id']}/{info['lakehouse_id']}/{relative}"
    token = info["storage_token"]

    def _call(suffix: str, method: str, body: bytes) -> None:
        request = urllib.request.Request(
            url + suffix, data=body, method=method, headers={"Authorization": f"Bearer {token}"}
        )
        urllib.request.urlopen(request, timeout=60)

    _call("?resource=file&overwrite=true", "PUT", b"")
    _call("?action=append&position=0", "PATCH", content)
    _call(f"?action=flush&position={len(content)}", "PATCH", b"")


def test_lakehouse_wipe_deletes_files_and_tables(tmp_path: Path, fabric_lakehouse_target) -> None:
    from weaver_runtime.dbrep.fabric import onelake

    workspace = fabric_lakehouse_target["workspace"]
    lakehouse = fabric_lakehouse_target["lakehouse"]
    info = onelake.resolve_lakehouse(workspace, lakehouse)

    # Seed a Files/T0 folder and a Tables/T1 table folder.
    _dfs_put(info, "Files/T0/Raw/Drop/drop.csv", b"a,b\n1,2\n")
    _dfs_put(info, "Tables/T1/Stage.Record/_delta_log/00.json", b"{}")

    weaver = write_config_files(
        tmp_path,
        {"LH": {"server": f"{workspace}/{lakehouse}", "platform": "fabric"}},
        {
            "T0_FILES": {"type": "Files", "server": "LH", "database": "T0"},
            "T1_DELTA": {"type": "Delta", "server": "LH", "database": "T1"},
        },
    )

    # Files wipe: present -> deleted -> absent.
    assert run_wipe(weaver, "T0_FILES")["existed"] is True
    assert run_wipe(weaver, "T0_FILES")["existed"] is False

    # Delta wipe: present -> deleted -> absent.
    assert run_wipe(weaver, "T1_DELTA")["existed"] is True
    assert run_wipe(weaver, "T1_DELTA")["existed"] is False
