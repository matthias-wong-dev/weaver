from __future__ import annotations

import textwrap
from pathlib import Path

from weaver_runtime.dbrep.ses.dependencies import (
    EXTERNAL,
    INTRA,
    MANAGED_CROSS,
    classify_object_dependencies,
    classify_reference,
)
from weaver_runtime.dbrep.ses.discovery import load_source_object


def test_two_part_reference_is_intra_database() -> None:
    dep = classify_reference(("Stage", "Record"), current_database="T1", managed_databases=set())
    assert dep.scope == INTRA
    assert dep.id == "T1.Stage.Record"


def test_three_part_reference_is_managed_when_database_supplied() -> None:
    dep = classify_reference(
        ("T0", "Raw", "Drop"), current_database="T1", managed_databases={"T0", "T1"}
    )
    assert dep.scope == MANAGED_CROSS
    assert dep.id == "T0.Raw.Drop"


def test_three_part_reference_is_external_when_database_not_supplied() -> None:
    dep = classify_reference(
        ("T9", "External", "Source"), current_database="T1", managed_databases={"T1"}
    )
    assert dep.scope == EXTERNAL
    assert dep.id == "T9.External.Source"


def test_three_part_reference_to_own_database_is_intra() -> None:
    dep = classify_reference(
        ("T1", "Stage", "Record"), current_database="T1", managed_databases={"T1"}
    )
    assert dep.scope == INTRA
    assert dep.id == "T1.Stage.Record"


def test_four_part_reference_is_external() -> None:
    dep = classify_reference(
        ("Srv", "T0", "Raw", "Drop"), current_database="T1", managed_databases={"T0", "T1"}
    )
    assert dep.scope == EXTERNAL
    assert dep.id == "Srv.T0.Raw.Drop"


def _write_python_object(path: Path) -> None:
    path.write_text(
        textwrap.dedent(
            '''
            """
            Table ID: Stage.Record
            Description: Normalised records.
            Lineage: Reads raw and prior.
            Primary key: record_id
            """

            from weaver_runtime.dbrep.objects import Table


            class Stage__Record(Table):
                def read(self, spark):
                    drop = self.repo["T0.Raw.Drop"]
                    prior = self.repo["Stage.Prior"]
                    ext = self.repo["T9.External.Source"]
                    return drop
            '''
        ),
        encoding="utf-8",
    )


def test_classify_object_dependencies_supplied(tmp_path: Path) -> None:
    path = tmp_path / "Stage__Record.py"
    _write_python_object(path)
    obj = load_source_object(path, "T1")

    deps = {dep.id: dep.scope for dep in classify_object_dependencies(obj, {"T0", "T1"})}
    assert deps == {
        "T0.Raw.Drop": MANAGED_CROSS,
        "T1.Stage.Prior": INTRA,
        "T9.External.Source": EXTERNAL,
    }


def test_classify_object_dependencies_when_cross_db_not_supplied(tmp_path: Path) -> None:
    path = tmp_path / "Stage__Record.py"
    _write_python_object(path)
    obj = load_source_object(path, "T1")

    # T0 is not supplied, so the cross-database dependency becomes external.
    deps = {dep.id: dep.scope for dep in classify_object_dependencies(obj, {"T1"})}
    assert deps["T0.Raw.Drop"] == EXTERNAL
    assert deps["T1.Stage.Prior"] == INTRA


def test_sql_object_dependencies(tmp_path: Path) -> None:
    path = tmp_path / "Mart.RecordAggregate.sql"
    path.write_text(
        textwrap.dedent(
            """
            /*
            Table ID: Mart.RecordAggregate
            Description: Aggregate.
            Lineage: Aggregates records.
            Primary key: group_id
            */
            select group_id, sum(amount) as total
            from Stage.Record
            join T0.Raw.Drop d on d.group_id = Stage.Record.group_id
            group by group_id
            """
        ),
        encoding="utf-8",
    )
    obj = load_source_object(path, "T2")
    deps = {dep.id: dep.scope for dep in classify_object_dependencies(obj, {"T0", "T2"})}
    assert deps["T2.Stage.Record"] == INTRA
    assert deps["T0.Raw.Drop"] == MANAGED_CROSS
