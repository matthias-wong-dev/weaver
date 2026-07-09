from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from dbrep_helpers import write_config_files
from fabric_helpers import manifest_ids, object_exists, view_rows
from weaver_runtime.cli import main

pytestmark = pytest.mark.fabric


def _write_ses(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "raw.Seed.sql").write_text(
        textwrap.dedent(
            """\
            /*
            Table ID: raw.Seed
            Description: Seed rows.
            Lineage: Emits a small fixed set of seed rows.
            Primary key: record_id
            */
            select record_id, group_id, amount
            from (values
                ('r1', 'A', 10),
                ('r2', 'A', 20),
                ('r3', 'B', 30)
            ) as v(record_id, group_id, amount)
            """
        ),
        encoding="utf-8",
    )
    (root / "mart.Aggregate.sql").write_text(
        textwrap.dedent(
            """\
            /*
            Table ID: mart.Aggregate
            Description: Amount per group.
            Lineage: Aggregates the seed rows by group.
            Primary key: group_id
            */
            select group_id, sum(amount) as amount
            from raw.Seed
            group by group_id
            """
        ),
        encoding="utf-8",
    )
    (root / "report.Summary.sql").write_text(
        textwrap.dedent(
            """\
            /*
            View ID: report.Summary
            Description: Summary view.
            Lineage: Reads the aggregate.
            */
            select group_id, amount from mart.Aggregate
            """
        ),
        encoding="utf-8",
    )


def _config(tmp_path: Path, target) -> Path:
    servers = {
        "SES_Repo": {"server": str(tmp_path / "SES")},
        "Warehouse": {"server": target["server"], "degrees_of_parallelism": target["dop"]},
    }
    databases = {
        "T2_SES": {"type": "SES", "server": "SES_Repo", "database": "T2"},
        "T2_PYTEST_SQL": {"type": "SQL", "server": "Warehouse", "database": target["database"]},
    }
    return write_config_files(tmp_path, servers, databases)


def test_sql_build_load_and_prune(tmp_path: Path, clean_fabric_sql, capsys) -> None:
    target = clean_fabric_sql
    server, database = target["server"], target["database"]
    ses = tmp_path / "SES" / "T2"
    _write_ses(ses)
    weaver = _config(tmp_path, target)

    # --- build -----------------------------------------------------------
    assert (
        main(["build", "--config", str(weaver), "--from", "T2_SES", "--to", "T2_PYTEST_SQL", "--prune"])
        == 0
    )
    capsys.readouterr()

    assert object_exists(server, database, "[raw].[Seed]", "V")
    assert object_exists(server, database, "[mart].[Aggregate]", "V")
    assert object_exists(server, database, "[report].[Summary]", "V")
    assert object_exists(server, database, "[raw].[Seed_Current]", "U")
    assert object_exists(server, database, "[_].[ETL raw.Seed]", "P")
    assert object_exists(server, database, "[_].[ETL mart.Aggregate]", "P")
    assert set(manifest_ids(server, database)) == {
        "T2.raw.Seed",
        "T2.mart.Aggregate",
        "T2.report.Summary",
    }

    # --- load ------------------------------------------------------------
    assert main(["load", "--config", str(weaver), "--target", "T2_PYTEST_SQL"]) == 0
    capsys.readouterr()

    summary = {row["group_id"]: row["amount"] for row in view_rows(server, database, "report.Summary")}
    assert summary == {"A": 30, "B": 30}
    seed = {row["record_id"]: row["amount"] for row in view_rows(server, database, "raw.Seed")}
    assert seed == {"r1": 10, "r2": 20, "r3": 30}

    # --- prune -----------------------------------------------------------
    (ses / "report.Summary.sql").unlink()
    assert (
        main(["build", "--config", str(weaver), "--from", "T2_SES", "--to", "T2_PYTEST_SQL", "--prune"])
        == 0
    )
    capsys.readouterr()

    assert not object_exists(server, database, "[report].[Summary]", "V")
    assert set(manifest_ids(server, database)) == {"T2.raw.Seed", "T2.mart.Aggregate"}

    # Data in the remaining managed objects survives an idempotent rebuild + reload.
    assert main(["load", "--config", str(weaver), "--target", "T2_PYTEST_SQL"]) == 0
    aggregate = {row["group_id"]: row["amount"] for row in view_rows(server, database, "mart.Aggregate")}
    assert aggregate == {"A": 30, "B": 30}
