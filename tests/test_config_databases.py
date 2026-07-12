from __future__ import annotations

from pathlib import Path

import pytest

from weaver_runtime.dbrep.config import (
    DatabasesConfig,
    load_databases_config,
    parse_databases_config,
    parse_environment_config,
)
from weaver_runtime.dbrep.errors import ConfigError


def _environment():
    return parse_environment_config(
        {
            "version": 1,
            "servers": {
                "SES_Repo": {"type": "SES", "server": "/repo/SES"},
                "Fabric_T1_Lakehouse": {
                    "type": "Fabric Lakehouse", "server": "Workspace/T1"
                },
                "Fabric_SQL_Server": {
                    "type": "SQL",
                    "server": "endpoint.example.fabric.microsoft.com",
                    "degrees_of_parallelism": 8,
                },
            },
        },
        base_dir="/cfg",
    )


def _databases_payload() -> dict:
    return {
        "version": 1,
        "databases": {
            "T0_SES": {"type": "SES", "server": "SES_Repo", "database": "T0"},
            "T0_LAKEHOUSE_FILES": {
                "type": "Files",
                "server": "Fabric_T1_Lakehouse",
                "database": "T0",
            },
            "T1_LAKEHOUSE_DELTA": {
                "type": "Delta",
                "server": "Fabric_T1_Lakehouse",
                "database": "T1",
            },
            "T2_WAREHOUSE_SQL": {
                "type": "SQL",
                "server": "Fabric_SQL_Server",
                "database": "T2",
            },
        },
    }


def test_parses_database_representations() -> None:
    config = parse_databases_config(_databases_payload(), _environment(), base_dir="/cfg")
    assert isinstance(config, DatabasesConfig)
    assert set(config.aliases()) == {
        "T0_SES",
        "T0_LAKEHOUSE_FILES",
        "T1_LAKEHOUSE_DELTA",
        "T2_WAREHOUSE_SQL",
    }


def test_type_belongs_to_the_database_representation() -> None:
    config = parse_databases_config(_databases_payload(), _environment(), base_dir="/cfg")
    assert config.get("T0_SES").type == "SES"
    assert config.get("T0_LAKEHOUSE_FILES").type == "Files"
    assert config.get("T1_LAKEHOUSE_DELTA").type == "Delta"
    assert config.get("T2_WAREHOUSE_SQL").type == "SQL"


def test_lakehouse_host_can_contain_files_and_delta_representations() -> None:
    config = parse_databases_config(_databases_payload(), _environment(), base_dir="/cfg")
    files = config.get("T0_LAKEHOUSE_FILES")
    delta = config.get("T1_LAKEHOUSE_DELTA")
    assert files.server == delta.server == "Fabric_T1_Lakehouse"
    assert files.type != delta.type


def test_database_environment_override_resolves_over_server_default() -> None:
    environment = _environment()
    fabric = environment.get("Fabric_T1_Lakehouse")
    object.__setattr__(fabric, "environment", "Default")
    payload = _databases_payload()
    payload["databases"]["T1_LAKEHOUSE_DELTA"]["environment"] = "Override"
    config = parse_databases_config(payload, environment, base_dir="/cfg")
    from weaver_runtime.dbrep.config import resolve_database

    resolved = resolve_database(config.get("T1_LAKEHOUSE_DELTA"), environment)
    assert resolved.environment == "Override"


def test_rejects_database_server_type_mismatch() -> None:
    payload = _databases_payload()
    payload["databases"]["T0_SES"]["server"] = "Fabric_SQL_Server"
    with pytest.raises(ConfigError, match="incompatible"):
        parse_databases_config(payload, _environment(), base_dir="/cfg")


def test_rejects_invalid_type() -> None:
    payload = _databases_payload()
    payload["databases"]["T0_SES"]["type"] = "Parquet"
    with pytest.raises(ConfigError, match="invalid type"):
        parse_databases_config(payload, _environment(), base_dir="/cfg")


def test_rejects_unknown_server_reference() -> None:
    payload = _databases_payload()
    payload["databases"]["T0_SES"]["server"] = "Nope"
    with pytest.raises(ConfigError, match="unknown server"):
        parse_databases_config(payload, _environment(), base_dir="/cfg")


def test_requires_type_server_database_keys() -> None:
    payload = _databases_payload()
    del payload["databases"]["T0_SES"]["database"]
    with pytest.raises(ConfigError, match="missing required keys"):
        parse_databases_config(payload, _environment(), base_dir="/cfg")


def test_load_databases_config_resolves_uses_environment(tmp_path: Path) -> None:
    (tmp_path / "env.yml").write_text(
        "version: 1\nservers:\n  Repo:\n    type: SES\n    server: SES\n",
        encoding="utf-8",
    )
    weaver_path = tmp_path / "weaver.yml"
    weaver_path.write_text(
        """
version: 1
uses:
  environment: env.yml
databases:
  T0_SES:
    type: SES
    server: Repo
    database: T0
""",
        encoding="utf-8",
    )

    config = load_databases_config(weaver_path)
    assert config.get("T0_SES").database == "T0"
    assert config.environment.get("Repo").server == "SES"
    assert config.environment.base_dir == tmp_path
