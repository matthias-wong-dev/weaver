from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from weaver_runtime.dbrep.errors import DiscoveryError
from weaver_runtime.dbrep.ses.discovery import (
    discover_database,
    discover_database_folders,
    discover_object_files,
    discover_runtime_objects,
    is_ignored,
    load_source_object,
)


def _python_object(schema: str, obj: str, *, extra: str = "") -> str:
    return textwrap.dedent(
        f'''
        """
        Table ID: {schema}.{obj}
        Description: {obj} table.
        Lineage: Builds {obj}.
        Primary key: record_id
        {extra}
        """

        class {schema}{obj}:
            def read(self, spark):
                return None
        '''
    )


def _folder_object(schema: str, obj: str) -> str:
    return textwrap.dedent(
        f'''
        """
        Folder ID: {schema}.{obj}
        Description: {obj} folder.
        Lineage: Writes {obj}.
        """

        class {schema}{obj}:
            def load(self):
                return None
        '''
    )


def _build_runtime_tree(root: Path) -> None:
    # Ignored host-level folders.
    (root / "_orchestrator").mkdir(parents=True)
    (root / "_orchestrator" / "weaver_load.py").write_text("# orchestrator\n", encoding="utf-8")
    (root / "_helpers").mkdir()
    (root / "_helpers" / "shared.py").write_text("SHARED = 1\n", encoding="utf-8")

    # Database folder T0 with a Folder object and an ignored helper folder.
    t0 = root / "T0"
    t0.mkdir()
    (t0 / "Raw__Drop.py").write_text(_folder_object("Raw", "Drop"), encoding="utf-8")
    (t0 / "_helpers").mkdir()
    (t0 / "_helpers" / "raw_helpers.py").write_text("HELP = 1\n", encoding="utf-8")

    # Database folder T1 with a Table object plus an ignored _private.py file.
    t1 = root / "T1"
    t1.mkdir()
    (t1 / "Stage__Record.py").write_text(_python_object("Stage", "Record"), encoding="utf-8")
    (t1 / "_private.py").write_text("X = 1\n", encoding="utf-8")


def test_is_ignored() -> None:
    assert is_ignored("_helpers")
    assert is_ignored("_orchestrator")
    assert not is_ignored("T0")


def test_discovers_database_folders_ignoring_underscore(tmp_path: Path) -> None:
    _build_runtime_tree(tmp_path)
    folders = [p.name for p in discover_database_folders(tmp_path)]
    assert folders == ["T0", "T1"]
    assert "_orchestrator" not in folders
    assert "_helpers" not in folders


def test_discovers_object_files_ignoring_underscore(tmp_path: Path) -> None:
    _build_runtime_tree(tmp_path)
    files = [p.name for p in discover_object_files(tmp_path / "T1")]
    assert files == ["Stage__Record.py"]
    # database-level _helpers folder and _private.py file are ignored.
    assert "_private.py" not in files


def test_load_source_object_normalises_id(tmp_path: Path) -> None:
    _build_runtime_tree(tmp_path)
    obj = load_source_object(tmp_path / "T1" / "Stage__Record.py", "T1")
    assert obj.declared_as == "Stage.Record"
    assert obj.id == "T1.Stage.Record"
    assert obj.kind == "Table"
    assert obj.language == "python"


def test_discover_runtime_objects_covers_all_databases(tmp_path: Path) -> None:
    _build_runtime_tree(tmp_path)
    objects = discover_runtime_objects(tmp_path)
    ids = sorted(o.id for o in objects)
    assert ids == ["T0.Raw.Drop", "T1.Stage.Record"]


def test_sql_object_file_discovery(tmp_path: Path) -> None:
    database = tmp_path / "T2"
    database.mkdir()
    (database / "Mart.RecordAggregate.sql").write_text(
        textwrap.dedent(
            """
            /*
            Table ID: Mart.RecordAggregate
            Description: Aggregate.
            Lineage: Aggregates records.
            Primary key: group_id
            */
            select group_id, sum(amount) as total from Stage.Record group by group_id
            """
        ),
        encoding="utf-8",
    )
    objects = discover_database(database, "T2")
    assert objects[0].id == "T2.Mart.RecordAggregate"
    assert objects[0].language == "sql"
    assert "group by" in (objects[0].sql_body or "")


def test_filename_must_match_declared_object(tmp_path: Path) -> None:
    database = tmp_path / "T1"
    database.mkdir()
    (database / "Wrong__Name.py").write_text(_python_object("Stage", "Record"), encoding="utf-8")
    with pytest.raises(DiscoveryError, match="must be named for its declared object"):
        discover_database(database, "T1")


def test_duplicate_object_across_python_and_sql_rejected(tmp_path: Path) -> None:
    database = tmp_path / "T1"
    database.mkdir()
    (database / "Stage__Record.py").write_text(_python_object("Stage", "Record"), encoding="utf-8")
    (database / "Stage.Record.sql").write_text(
        textwrap.dedent(
            """
            /*
            Table ID: Stage.Record
            Description: dup.
            Lineage: dup.
            Primary key: record_id
            */
            select 1 as record_id
            """
        ),
        encoding="utf-8",
    )
    with pytest.raises(DiscoveryError, match="duplicate object"):
        discover_database(database, "T1")
