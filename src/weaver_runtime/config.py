from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


class WeaverConfigError(ValueError):
    """Raised when a Weaver configuration file is missing or invalid."""


@dataclass(frozen=True)
class CapacityConfig:
    resource_group: str
    name: str


@dataclass(frozen=True)
class WorkspaceConfig:
    name: str | None
    source: Path | None


@dataclass(frozen=True)
class RepositoryConfig:
    name: str
    source: Path
    target: str


@dataclass(frozen=True)
class LakehouseConfig:
    workspace: str | None
    name: str | None
    target_root: str
    repositories: tuple[RepositoryConfig, ...]


@dataclass(frozen=True)
class SesConfig:
    source: Path
    server: str
    database: str


@dataclass(frozen=True)
class FabricConfig:
    capacity: CapacityConfig | None
    workspace: WorkspaceConfig | None
    lakehouse: LakehouseConfig | None
    ses: SesConfig | None


@dataclass(frozen=True)
class WeaverConfig:
    path: Path
    version: int
    fabric: FabricConfig

    @property
    def base_dir(self) -> Path:
        return self.path.parent


def load_weaver_config(path: str | Path) -> WeaverConfig:
    """Load a Weaver YAML config, resolving relative paths from the file."""

    config_path = Path(path).expanduser().resolve()
    if not config_path.exists():
        raise WeaverConfigError(f"config not found: {config_path}")

    payload = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict):
        raise WeaverConfigError("config must be a YAML mapping")

    version = payload.get("version")
    if version != 1:
        raise WeaverConfigError(f"unsupported config version: {version!r}")

    fabric = _mapping(payload, "fabric", required=False) or {}
    return WeaverConfig(
        path=config_path,
        version=version,
        fabric=FabricConfig(
            capacity=_capacity_config(fabric),
            workspace=_workspace_config(config_path, fabric),
            lakehouse=_lakehouse_config(config_path, fabric),
            ses=_ses_config(config_path, fabric),
        ),
    )


def _capacity_config(fabric: dict[str, Any]) -> CapacityConfig | None:
    raw = _mapping(fabric, "capacity", required=False)
    if raw is None:
        return None
    return CapacityConfig(
        resource_group=_string(raw, "resource_group", "fabric.capacity.resource_group"),
        name=_string(raw, "name", "fabric.capacity.name"),
    )


def _workspace_config(config_path: Path, fabric: dict[str, Any]) -> WorkspaceConfig | None:
    raw = _mapping(fabric, "workspace", required=False)
    if raw is None:
        return None
    return WorkspaceConfig(
        name=_optional_string(raw, "name", "fabric.workspace.name"),
        source=_optional_path(config_path, raw, "source"),
    )


def _lakehouse_config(config_path: Path, fabric: dict[str, Any]) -> LakehouseConfig | None:
    raw = _mapping(fabric, "lakehouse", required=False)
    if raw is None:
        return None

    repositories = []
    raw_repositories = raw.get("repositories") or []
    if not isinstance(raw_repositories, list):
        raise WeaverConfigError("fabric.lakehouse.repositories must be a list")
    for index, entry in enumerate(raw_repositories):
        if not isinstance(entry, dict):
            raise WeaverConfigError(f"fabric.lakehouse.repositories[{index}] must be a mapping")
        repositories.append(
            RepositoryConfig(
                name=_string(entry, "name", f"fabric.lakehouse.repositories[{index}].name"),
                source=_path(config_path, entry, "source", f"fabric.lakehouse.repositories[{index}].source"),
                target=_string(entry, "target", f"fabric.lakehouse.repositories[{index}].target"),
            )
        )

    return LakehouseConfig(
        workspace=_optional_string(raw, "workspace", "fabric.lakehouse.workspace"),
        name=_optional_string(raw, "name", "fabric.lakehouse.name"),
        target_root=_string(raw, "target_root", "fabric.lakehouse.target_root"),
        repositories=tuple(repositories),
    )


def _ses_config(config_path: Path, fabric: dict[str, Any]) -> SesConfig | None:
    raw = _mapping(fabric, "ses", required=False)
    if raw is None:
        return None
    return SesConfig(
        source=_path(config_path, raw, "source", "fabric.ses.source"),
        server=_string(raw, "server", "fabric.ses.server"),
        database=_string(raw, "database", "fabric.ses.database"),
    )


def _mapping(payload: dict[str, Any], key: str, *, required: bool) -> dict[str, Any] | None:
    value = payload.get(key)
    if value is None:
        if required:
            raise WeaverConfigError(f"{key} is required")
        return None
    if not isinstance(value, dict):
        raise WeaverConfigError(f"{key} must be a mapping")
    return value


def _string(payload: dict[str, Any], key: str, label: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise WeaverConfigError(f"{label} must be a non-empty string")
    return value.strip()


def _optional_string(payload: dict[str, Any], key: str, label: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise WeaverConfigError(f"{label} must be a non-empty string when provided")
    return value.strip()


def _path(config_path: Path, payload: dict[str, Any], key: str, label: str) -> Path:
    return _resolve_path(config_path, _string(payload, key, label))


def _optional_path(config_path: Path, payload: dict[str, Any], key: str) -> Path | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise WeaverConfigError(f"{key} must be a non-empty string when provided")
    return _resolve_path(config_path, value)


def _resolve_path(config_path: Path, value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = config_path.parent / path
    return path.resolve()
