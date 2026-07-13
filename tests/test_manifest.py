from __future__ import annotations

from pathlib import Path

from dbrep_helpers import make_config, resolve, write_python_folder, write_python_table
from weaver_runtime.dbrep.build import BuildPair, BuildRequest, plan_build
from weaver_runtime.dbrep.build.manifest import (
    build_catalogue,
    build_column_dictionary,
    build_index_dictionary,
    build_load_dependency,
    build_manifest,
    build_table_dictionary,
)


def _plan(tmp_path: Path):
    ses_root = tmp_path / "SES"
    servers = {
        "SES_Repo": {"server": str(ses_root)},
        "Lake": {"server": str(tmp_path / "lake")},
    }
    databases = {
        "T0_SES": {"type": "SES", "server": "SES_Repo", "database": "T0"},
        "T1_SES": {"type": "SES", "server": "SES_Repo", "database": "T1"},
        "T0_FILES": {"type": "Files", "server": "Lake", "database": "T0"},
        "T1_DELTA": {"type": "Delta", "server": "Lake", "database": "T1"},
    }
    config = make_config(tmp_path, servers, databases)
    write_python_folder(ses_root / "T0", "Raw", "Drop")
    write_python_table(
        ses_root / "T1",
        "Stage",
        "Record",
        deps=("T0.Raw.Drop",),
        schema_cols=(("record_id", "string"), ("amount", "decimal(18,2)")),
    )
    write_python_table(ses_root / "T1", "Mart", "RecordCurrent", deps=("Stage.Record",))
    write_python_table(ses_root / "T1", "Mart", "Reference", primary_key="id", static=True)
    return plan_build(
        BuildRequest(
            pairs=(
                BuildPair(resolve(config, "T0_SES"), resolve(config, "T0_FILES")),
                BuildPair(resolve(config, "T1_SES"), resolve(config, "T1_DELTA")),
            )
        )
    )


def test_manifest_is_provenance_only(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    manifest = build_manifest(
        plan.objects,
        target_server="Lake",
        installed_from=[o.source_alias for o in plan.objects],
        installed_to=[o.target_alias for o in plan.objects],
        external_dependencies=plan.external_dependencies,
        installed_at="2026-07-09T00:00:00Z",
        version="9.9.9",
    )

    assert manifest["target_server"] == "Lake"
    assert manifest["runtime_root"] == "Files/_weaver/runtime"
    assert manifest["built_at"] == "2026-07-09T00:00:00Z"
    assert manifest["weaver_version"] == "9.9.9"
    assert manifest["object_count"] == 4
    assert "objects" not in manifest


def test_catalogue_records_objects_and_source_hashes(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    catalogue = build_catalogue(plan.objects)

    by_id = {entry["id"]: entry for entry in catalogue["objects"]}
    assert by_id["T0.Raw.Drop"]["kind"] == "Folder"
    assert by_id["T0.Raw.Drop"]["materialisation"] == "Files/T0/Raw/Drop"
    assert by_id["T1.Stage.Record"]["installed_source"] == "objects/T1/Stage__Record.py"
    assert len(by_id["T1.Stage.Record"]["source_hash"]) == 64
    assert by_id["T1.Mart.Reference"]["static"] is True


def test_load_dependency_records_graph(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    load_dependency = build_load_dependency(plan.objects)

    assert load_dependency["objects"]["T1.Stage.Record"] == ["T0.Raw.Drop"]
    assert load_dependency["objects"]["T1.Mart.RecordCurrent"] == ["T1.Stage.Record"]
    assert load_dependency["objects"]["T1.Mart.Reference"] == []


def test_dictionary_artifacts_record_metadata(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    table_dictionary = build_table_dictionary(plan.objects)
    column_dictionary = build_column_dictionary(plan.objects)
    index_dictionary = build_index_dictionary(plan.objects)

    tables = {entry["id"]: entry for entry in table_dictionary["tables"]}
    assert tables["T1.Mart.Reference"]["static"] is True
    assert tables["T1.Stage.Record"]["load_mode"] == "upsert"
    assert tables["T1.Stage.Record"]["is_incremental"] is False
    assert all("auto_delete" not in entry for entry in tables.values())

    columns = [
        (entry["ordinal"], entry["column"], entry["type"])
        for entry in column_dictionary["columns"]
        if entry["object_id"] == "T1.Stage.Record"
    ]
    assert columns == [(1, "record_id", "string"), (2, "amount", "decimal(18,2)")]

    indexes = {entry["object_id"]: entry for entry in index_dictionary["indexes"]}
    assert indexes["T1.Stage.Record"]["columns"] == ["record_id"]
