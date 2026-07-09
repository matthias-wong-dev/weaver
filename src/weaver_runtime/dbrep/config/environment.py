"""Environment config: host/server declarations only.

The environment file declares *where* things live (fourth-level hosts). It does
not declare database representations, object types, or prod/dev/test. Users pick
between environments by choosing different config files.

Example::

    version: 1
    servers:
      Repo:
        server: /path/to/repo/SES
      Local_Lakehouse:
        server: .local/lakehouse/Warehouse
      Warehouse_SQL:
        server: endpoint.example.fabric.microsoft.com
        degrees_of_parallelism: 8
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from ..errors import ConfigError

_ALLOWED_SERVER_KEYS = {"server", "degrees_of_parallelism", "platform"}
_ALLOWED_PLATFORMS = {"local", "fabric"}


@dataclass(frozen=True)
class ServerConfig:
    """A single host/server alias.

    ``server`` is a raw host value. Its meaning depends on the database
    representation that points at it: a filesystem parent for SES, a Lakehouse
    host for Files/Delta, or a SQL endpoint for SQL.

    ``platform`` is ``local`` (filesystem, the default) or ``fabric`` (OneLake /
    Fabric Spark). For a Fabric Lakehouse host, ``server`` is ``Workspace/Lakehouse``.
    """

    alias: str
    server: str
    degrees_of_parallelism: int | None = None
    platform: str = "local"


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

    unknown = set(raw) - _ALLOWED_SERVER_KEYS
    if unknown:
        raise ConfigError(
            f"server {alias!r} has keys that do not belong in environment config: "
            + ", ".join(sorted(unknown))
        )

    server = raw.get("server")
    if not isinstance(server, str) or not server.strip():
        raise ConfigError(f"server {alias!r} must define a non-empty 'server' value")

    dop = raw.get("degrees_of_parallelism")
    if dop is not None:
        if isinstance(dop, bool) or not isinstance(dop, int) or dop < 1:
            raise ConfigError(
                f"server {alias!r} degrees_of_parallelism must be a positive integer"
            )

    platform = raw.get("platform", "local")
    if platform not in _ALLOWED_PLATFORMS:
        raise ConfigError(
            f"server {alias!r} platform must be one of {', '.join(sorted(_ALLOWED_PLATFORMS))}"
        )

    return ServerConfig(
        alias=alias,
        server=server.strip(),
        degrees_of_parallelism=dop,
        platform=platform,
    )
