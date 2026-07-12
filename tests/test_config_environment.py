from __future__ import annotations

from pathlib import Path

import pytest

from weaver_runtime.dbrep.config import (
    EnvironmentConfig,
    load_environment_config,
    parse_environment_config,
)
from weaver_runtime.dbrep.errors import ConfigError


def _env_payload() -> dict:
    return {
        "version": 1,
        "servers": {
            "SES_Repo": {"type": "SES", "server": "/path/to/repo/SES"},
            "Local_Lakehouse": {"type": "Local Lakehouse", "server": ".local/lakehouse/T1"},
            "Fabric_SQL_Server": {
                "type": "SQL",
                "server": "endpoint.example.fabric.microsoft.com",
                "degrees_of_parallelism": 8,
            },
        },
    }


def test_parses_servers_and_aliases() -> None:
    env = parse_environment_config(_env_payload(), base_dir="/tmp")
    assert set(env.aliases()) == {"SES_Repo", "Local_Lakehouse", "Fabric_SQL_Server"}
    assert env.get("SES_Repo").server == "/path/to/repo/SES"


def test_server_requires_explicit_type() -> None:
    env = parse_environment_config(_env_payload(), base_dir="/tmp")
    ses_repo = env.get("SES_Repo")
    assert ses_repo.degrees_of_parallelism is None
    assert ses_repo.type == "SES"


def test_sql_server_degrees_of_parallelism_is_parsed() -> None:
    env = parse_environment_config(_env_payload(), base_dir="/tmp")
    assert env.get("Fabric_SQL_Server").degrees_of_parallelism == 8


def test_fabric_lakehouse_fields_and_environment_are_parsed() -> None:
    payload = _env_payload()
    payload["servers"]["Fabric"] = {
        "type": "Fabric Lakehouse",
        "server": "Workspace/Lakehouse",
        "environment": "Python Libraries",
    }
    server = parse_environment_config(payload, base_dir="/tmp").get("Fabric")
    assert (server.server, server.environment) == ("Workspace/Lakehouse", "Python Libraries")


def test_fabric_lakehouse_rejects_malformed_server_field() -> None:
    payload = _env_payload()
    payload["servers"]["Fabric"] = {
        "type": "Fabric Lakehouse", "server": "LakehouseOnly"
    }
    with pytest.raises(ConfigError, match="Workspace/Lakehouse"):
        parse_environment_config(payload, base_dir="/tmp")


def test_rejects_invalid_server_type() -> None:
    payload = _env_payload()
    payload["servers"]["SES_Repo"]["type"] = "Lakehouse"
    with pytest.raises(ConfigError, match="type must be one of"):
        parse_environment_config(payload, base_dir="/tmp")


def test_database_property_does_not_belong_in_environment() -> None:
    payload = _env_payload()
    payload["servers"]["SES_Repo"]["database"] = "T1"
    with pytest.raises(ConfigError, match="do not belong in environment"):
        parse_environment_config(payload, base_dir="/tmp")


def test_rejects_non_positive_degrees_of_parallelism() -> None:
    payload = _env_payload()
    payload["servers"]["Fabric_SQL_Server"]["degrees_of_parallelism"] = 0
    with pytest.raises(ConfigError, match="degrees_of_parallelism"):
        parse_environment_config(payload, base_dir="/tmp")


def test_rejects_unknown_version() -> None:
    with pytest.raises(ConfigError, match="unsupported environment config version"):
        parse_environment_config({"version": 2, "servers": {}}, base_dir="/tmp")


def test_requires_non_empty_servers() -> None:
    with pytest.raises(ConfigError, match="non-empty 'servers'"):
        parse_environment_config({"version": 1, "servers": {}}, base_dir="/tmp")


def test_load_from_file_sets_base_dir(tmp_path: Path) -> None:
    env_path = tmp_path / "env.yml"
    env_path.write_text(
        "version: 1\nservers:\n  Repo:\n    type: SES\n    server: SES\n",
        encoding="utf-8",
    )
    env = load_environment_config(env_path)
    assert isinstance(env, EnvironmentConfig)
    assert env.base_dir == tmp_path
    assert env.get("Repo").server == "SES"
