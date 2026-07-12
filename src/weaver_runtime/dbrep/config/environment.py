"""Environment config: host/server declarations only.

The environment file declares *where* things live (fourth-level hosts). It does
not declare database representations, object types, or prod/dev/test. Users pick
between environments by choosing different config files.

Example::

    version: 1
    servers:
      Repo:
        type: SES
        server: /path/to/repo/SES
      Local_Lakehouse:
        type: Local Lakehouse
        server: .local/lakehouse/Warehouse
      Fabric_Lakehouse:
        type: Fabric Lakehouse
        server: Workspace/Warehouse
        environment: Python Libraries
      Warehouse_SQL:
        type: SQL
        server: endpoint.example.fabric.microsoft.com
        degrees_of_parallelism: 8
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from ..errors import ConfigError

SES_SERVER = "SES"
LOCAL_LAKEHOUSE_SERVER = "Local Lakehouse"
FABRIC_LAKEHOUSE_SERVER = "Fabric Lakehouse"
SQL_SERVER = "SQL"
SERVER_TYPES = frozenset(
    {SES_SERVER, LOCAL_LAKEHOUSE_SERVER, FABRIC_LAKEHOUSE_SERVER, SQL_SERVER}
)
_COMMON_KEYS = {"type", "degrees_of_parallelism"}
_TYPE_KEYS = {
    SES_SERVER: {"server"},
    LOCAL_LAKEHOUSE_SERVER: {"server"},
    FABRIC_LAKEHOUSE_SERVER: {"server", "environment"},
    SQL_SERVER: {"server"},
}


@dataclass(frozen=True)
class ServerConfig:
    """A single host/server alias.

    The explicit ``type`` determines whether ``server`` is a local path or SQL
    endpoint, local path, or encoded ``Workspace/Lakehouse`` Fabric host.
    """

    alias: str
    type: str
    server: str | None = None
    environment: str | None = None
    degrees_of_parallelism: int | None = None


@dataclass(frozen=True)
class EnvironmentConfig:
    """Parsed environment config plus the directory it was loaded from."""

    version: int
    servers: tuple[ServerConfig, ...]
    base_dir: Path

    def has(self, alias: str) -> bool:
        return any(server.alias == alias for server in self.servers)

    def get(self, alias: str) -> ServerConfig:
        for server in self.servers:
            if server.alias == alias:
                return server
        raise ConfigError(f"unknown server alias: {alias!r}")

    def aliases(self) -> tuple[str, ...]:
        return tuple(server.alias for server in self.servers)


def load_environment_config(path: str | Path) -> EnvironmentConfig:
    """Load and parse an environment config file."""

    config_path = Path(path).expanduser().resolve()
    if not config_path.is_file():
        raise ConfigError(f"environment config not found: {config_path}")
    payload = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    return parse_environment_config(payload, base_dir=config_path.parent)


def parse_environment_config(payload: Any, base_dir: str | Path) -> EnvironmentConfig:
    """Parse an already-loaded environment mapping."""

    if not isinstance(payload, dict):
        raise ConfigError("environment config must be a mapping")

    version = payload.get("version")
    if version != 1:
        raise ConfigError(f"unsupported environment config version: {version!r}")

    raw_servers = payload.get("servers")
    if not isinstance(raw_servers, dict) or not raw_servers:
        raise ConfigError("environment config must define a non-empty 'servers' mapping")

    servers: list[ServerConfig] = []
    for alias, raw in raw_servers.items():
        servers.append(_parse_server(str(alias), raw))

    return EnvironmentConfig(
        version=version,
        servers=tuple(servers),
        base_dir=Path(base_dir),
    )


def _parse_server(alias: str, raw: Any) -> ServerConfig:
    if not isinstance(raw, dict):
        raise ConfigError(f"server {alias!r} must be a mapping")

    type_value = raw.get("type")
    if type_value not in SERVER_TYPES:
        raise ConfigError(
            f"server {alias!r} type must be one of {', '.join(sorted(SERVER_TYPES))}"
        )

    allowed = _COMMON_KEYS | _TYPE_KEYS[type_value]
    unknown = set(raw) - allowed
    if unknown:
        raise ConfigError(
            f"server {alias!r} has keys that do not belong in environment config: "
            + ", ".join(sorted(unknown))
        )

    values: dict[str, str | None] = {}
    required = ("server",)
    for key in required:
        value = raw.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ConfigError(f"server {alias!r} must define a non-empty {key!r} value")
        values[key] = value.strip()
    if type_value == FABRIC_LAKEHOUSE_SERVER:
        parts = values["server"].split("/", 1)
        if len(parts) != 2 or not all(part.strip() for part in parts):
            raise ConfigError(
                f"server {alias!r} must use a non-empty 'Workspace/Lakehouse' value"
            )
    environment = raw.get("environment")
    if environment is not None and (not isinstance(environment, str) or not environment.strip()):
        raise ConfigError(f"server {alias!r} environment must be a non-empty string")

    dop = raw.get("degrees_of_parallelism")
    if dop is not None:
        if isinstance(dop, bool) or not isinstance(dop, int) or dop < 1:
            raise ConfigError(
                f"server {alias!r} degrees_of_parallelism must be a positive integer"
            )

    return ServerConfig(
        alias=alias,
        type=type_value,
        server=values.get("server"),
        environment=environment.strip() if environment else None,
        degrees_of_parallelism=dop,
    )
