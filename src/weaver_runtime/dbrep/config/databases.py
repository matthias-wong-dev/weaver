"""Database representation config: what Weaver builds from and to.

There is no source/target split. Both SES folders and live destinations are
database representations. The build command decides direction.

Example::

    version: 1
    uses:
      environment: env.yml
    databases:
      Drop_SES:
        type: SES
        server: Repo
        database: Drop
      Drop_LAKEHOUSE_FILES:
        type: Files
        server: Local_Lakehouse
        database: Drop
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from ..errors import ConfigError
from .environment import (
    FABRIC_LAKEHOUSE_SERVER,
    LOCAL_LAKEHOUSE_SERVER,
    SES_SERVER,
    SQL_SERVER,
    EnvironmentConfig,
    load_environment_config,
    parse_environment_config,
)

SES = "SES"
FILES = "Files"
DELTA = "Delta"
SQL = "SQL"
DATABASE_TYPES = frozenset({SES, FILES, DELTA, SQL})

_REQUIRED_DATABASE_KEYS = {"type", "server", "database"}
_ALLOWED_DATABASE_KEYS = _REQUIRED_DATABASE_KEYS | {"environment"}
_COMPATIBLE_SERVER_TYPES = {
    SES: {SES_SERVER}, FILES: {LOCAL_LAKEHOUSE_SERVER, FABRIC_LAKEHOUSE_SERVER},
    DELTA: {LOCAL_LAKEHOUSE_SERVER, FABRIC_LAKEHOUSE_SERVER}, SQL: {SQL_SERVER},
}


@dataclass(frozen=True)
class DatabaseConfig:
    """A single named database representation.

    ``type`` belongs to the representation (not the host). ``server`` names a
    host alias in the environment config. ``database`` is the third-level name.
    """

    alias: str
    type: str
    server: str
    database: str
    environment: str | None = None


@dataclass(frozen=True)
class DatabasesConfig:
    """Parsed database representations, bound to their environment."""

    version: int
    environment: EnvironmentConfig
    databases: tuple[DatabaseConfig, ...]
    base_dir: Path

    def has(self, alias: str) -> bool:
        return any(database.alias == alias for database in self.databases)

    def get(self, alias: str) -> DatabaseConfig:
        for database in self.databases:
            if database.alias == alias:
                return database
        raise ConfigError(f"unknown database representation: {alias!r}")

    def aliases(self) -> tuple[str, ...]:
        return tuple(database.alias for database in self.databases)


def load_databases_config(path: str | Path) -> DatabasesConfig:
    """Load database config and the environment it references via ``uses``."""

    config_path = Path(path).expanduser().resolve()
    if not config_path.is_file():
        raise ConfigError(f"database config not found: {config_path}")

    payload = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict):
        raise ConfigError("database config must be a mapping")

    environment = _load_referenced_environment(payload, config_path)
    return parse_databases_config(payload, environment, base_dir=config_path.parent)


def parse_databases_config(
    payload: Any,
    environment: EnvironmentConfig,
    base_dir: str | Path,
) -> DatabasesConfig:
    """Parse an already-loaded database mapping against an environment."""

    if not isinstance(payload, dict):
        raise ConfigError("database config must be a mapping")

    version = payload.get("version")
    if version != 1:
        raise ConfigError(f"unsupported database config version: {version!r}")

    raw_databases = payload.get("databases")
    if not isinstance(raw_databases, dict) or not raw_databases:
        raise ConfigError("database config must define a non-empty 'databases' mapping")

    databases: list[DatabaseConfig] = []
    for alias, raw in raw_databases.items():
        databases.append(_parse_database(str(alias), raw, environment))

    return DatabasesConfig(
        version=version,
        environment=environment,
        databases=tuple(databases),
        base_dir=Path(base_dir),
    )


def _load_referenced_environment(payload: dict[str, Any], config_path: Path) -> EnvironmentConfig:
    uses = payload.get("uses")
    inline = payload.get("environment")
    if isinstance(uses, dict) and uses.get("environment"):
        env_ref = uses["environment"]
        if not isinstance(env_ref, str) or not env_ref.strip():
            raise ConfigError("uses.environment must be a path string")
        env_path = Path(env_ref).expanduser()
        if not env_path.is_absolute():
            env_path = config_path.parent / env_path
        return load_environment_config(env_path)
    if isinstance(inline, dict):
        return parse_environment_config(inline, base_dir=config_path.parent)
    raise ConfigError(
        "database config must reference an environment via 'uses.environment' "
        "or embed one under 'environment'"
    )


def _parse_database(alias: str, raw: Any, environment: EnvironmentConfig) -> DatabaseConfig:
    if not isinstance(raw, dict):
        raise ConfigError(f"database {alias!r} must be a mapping")

    missing = _REQUIRED_DATABASE_KEYS - set(raw)
    if missing:
        raise ConfigError(
            f"database {alias!r} is missing required keys: " + ", ".join(sorted(missing))
        )
    unknown = set(raw) - _ALLOWED_DATABASE_KEYS
    if unknown:
        raise ConfigError(f"database {alias!r} has unknown keys: {', '.join(sorted(unknown))}")

    type_value = raw.get("type")
    if type_value not in DATABASE_TYPES:
        raise ConfigError(
            f"database {alias!r} has invalid type {type_value!r}; "
            f"expected one of {', '.join(sorted(DATABASE_TYPES))}"
        )

    server = raw.get("server")
    if not isinstance(server, str) or not server.strip():
        raise ConfigError(f"database {alias!r} must name a server alias")
    if not environment.has(server.strip()):
        raise ConfigError(
            f"database {alias!r} references unknown server {server.strip()!r}"
        )
    server_config = environment.get(server.strip())
    if server_config.type not in _COMPATIBLE_SERVER_TYPES[type_value]:
        raise ConfigError(
            f"database {alias!r} type {type_value!r} is incompatible with "
            f"server {server.strip()!r} type {server_config.type!r}"
        )

    database = raw.get("database")
    if not isinstance(database, str) or not database.strip():
        raise ConfigError(f"database {alias!r} must define a non-empty 'database' name")

    environment_name = raw.get("environment")
    if environment_name is not None:
        if not isinstance(environment_name, str) or not environment_name.strip():
            raise ConfigError(f"database {alias!r} environment must be a non-empty string")
        if type_value not in (FILES, DELTA) or server_config.type != FABRIC_LAKEHOUSE_SERVER:
            raise ConfigError(
                f"database {alias!r} environment is valid only for Files or Delta "
                "on a Fabric Lakehouse server"
            )

    return DatabaseConfig(
        alias=alias,
        type=type_value,
        server=server.strip(),
        database=database.strip(),
        environment=environment_name.strip() if environment_name else None,
    )
