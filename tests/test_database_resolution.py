from __future__ import annotations

from pathlib import Path

from weaver_runtime.dbrep.config import (
    delta_materialisation,
    delta_table_path,
    files_materialisation,
    files_object_path,
    parse_databases_config,
    parse_environment_config,
    resolve_all,
    resolve_database,
    runtime_root,
    ses_source_root,
    sql_identity,
)
from weaver_runtime.dbrep.config.resolution import filesystem_host


def _config(base_dir: str = "/cfg"):
    environment = parse_environment_config(
        {
            "version": 1,
            "servers": {
                "SES_Repo": {"server": "/path/to/repo/SES"},
                "Local_Lakehouse": {"server": ".local/lakehouse/T1"},
                "Fabric_SQL_Server": {
                    "server": "endpoint.example.fabric.microsoft.com",
                    "degrees_of_parallelism": 8,
                },
                "Fabric_Lakehouse": {"server": "Workspace/T1", "platform": "fabric"},
            },
        },
        base_dir=base_dir,
    )
    databases = parse_databases_config(
        {
            "version": 1,
            "databases": {
                "T1_SES": {"type": "SES", "server": "SES_Repo", "database": "T1"},
                "T0_LOCAL_FILES": {
                    "type": "Files",
                    "server": "Local_Lakehouse",
                    "database": "T0",
                },
                "T1_LOCAL_DELTA": {
                    "type": "Delta",
                    "server": "Local_Lakehouse",
                    "database": "T1",
                },
                "T2_WAREHOUSE_SQL": {
                    "type": "SQL",
                    "server": "Fabric_SQL_Server",
                    "database": "T2",
                },
                "T1_FABRIC_DELTA": {
                    "type": "Delta",
                    "server": "Fabric_Lakehouse",
                    "database": "T1",
                },
            },
        },
        environment,
        base_dir=base_dir,
    )
    return databases


def test_ses_path_resolves_to_host_slash_database() -> None:
    config = _config()
    resolved = resolve_database(config.get("T1_SES"), config.environment)
    assert ses_source_root(resolved) == Path("/path/to/repo/SES/T1")


def test_lakehouse_host_resolution_is_relative_to_base_dir() -> None:
    config = _config(base_dir="/cfg")
    resolved = resolve_database(config.get("T0_LOCAL_FILES"), config.environment)
    assert filesystem_host(resolved) == Path("/cfg/.local/lakehouse/T1")


def test_files_and_delta_colocate_under_same_lakehouse_host() -> None:
    config = _config()
    files = resolve_database(config.get("T0_LOCAL_FILES"), config.environment)
    delta = resolve_database(config.get("T1_LOCAL_DELTA"), config.environment)
    # Same Lakehouse host means one shared runtime root for Files and Delta.
    assert runtime_root(files) == runtime_root(delta)
    assert runtime_root(delta) == Path("/cfg/.local/lakehouse/T1/Files/_weaver/runtime")


def test_files_object_path_and_materialisation() -> None:
    config = _config()
    resolved = resolve_database(config.get("T0_LOCAL_FILES"), config.environment)
    assert files_object_path(resolved, "Raw", "Drop") == Path(
        "/cfg/.local/lakehouse/T1/Files/T0/Raw/Drop"
    )
    assert files_materialisation("T0", "Raw", "Drop") == "Files/T0/Raw/Drop"


def test_delta_table_path_and_materialisation() -> None:
    config = _config()
    # Local: schema and object are separate path components under the database.
    local = resolve_database(config.get("T1_LOCAL_DELTA"), config.environment)
    assert delta_table_path(local, "Stage", "Record") == Path(
        "/cfg/.local/lakehouse/T1/Tables/T1/Stage/Record"
    )
    assert delta_materialisation("T1", "Stage", "Record") == "Tables/T1/Stage/Record"
    assert delta_materialisation("T1", "Mart", "RecordAudit") == "Tables/T1/Mart/RecordAudit"


def test_fabric_delta_materialisation_omits_database() -> None:
    config = _config()
    fabric = resolve_database(config.get("T1_FABRIC_DELTA"), config.environment)
    # The Fabric Lakehouse is the database host: no database path component.
    assert delta_table_path(fabric, "Stage", "Record") == (
        filesystem_host(fabric) / "Tables" / "Stage" / "Record"
    )
    assert delta_materialisation("T1", "Stage", "Record", fabric=True) == "Tables/Stage/Record"
    assert delta_materialisation("T1", "Mart", "RecordAudit", fabric=True) == "Tables/Mart/RecordAudit"


def test_no_delta_materialisation_uses_a_dotted_schema_object_component() -> None:
    for fabric in (False, True):
        for schema, obj in (("Stage", "Record"), ("Mart", "RecordAudit")):
            path = delta_materialisation("T1", schema, obj, fabric=fabric)
            assert f"{schema}.{obj}" not in path
            assert f"{schema}/{obj}" in path


def test_sql_identity_and_degrees_of_parallelism() -> None:
    config = _config()
    resolved = resolve_database(config.get("T2_WAREHOUSE_SQL"), config.environment)
    identity = sql_identity(resolved, "Mart", "RecordAggregate")
    assert identity.server == "endpoint.example.fabric.microsoft.com"
    assert identity.database == "T2"
    assert identity.schema == "Mart"
    assert identity.object == "RecordAggregate"
    assert resolved.degrees_of_parallelism == 8


def test_resolve_all_returns_every_alias() -> None:
    config = _config()
    resolved = resolve_all(config)
    assert set(resolved) == {
        "T1_SES",
        "T0_LOCAL_FILES",
        "T1_LOCAL_DELTA",
        "T2_WAREHOUSE_SQL",
        "T1_FABRIC_DELTA",
    }
