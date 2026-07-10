"""Fabric connection settings: technical defaults and resolution.

These are environment/connection values (API URLs, auth scopes, Livy API
version, default parallelism). They are *not* product/operation config: no
workspace, lakehouse, repository, notebook, or endpoint names live here.

Resolution order for every field is::

    CLI override -> environment config -> technical fallback default
"""

from __future__ import annotations

import os
from dataclasses import dataclass


DEFAULT_API_BASE_URL = "https://api.fabric.microsoft.com"
DEFAULT_ONELAKE_BASE_URL = "https://onelake.dfs.fabric.microsoft.com"
DEFAULT_FABRIC_SCOPE = "https://analysis.windows.net/powerbi/api/.default"
DEFAULT_STORAGE_SCOPE = "https://storage.azure.com/.default"
DEFAULT_SQL_SCOPE = "https://database.windows.net/.default"
DEFAULT_LIVY_API_VERSION = "2023-12-01"
DEFAULT_DEGREES_OF_PARALLELISM = 32


@dataclass(frozen=True)
class FabricSettings:
    """Resolved Fabric connection settings for one operation."""

    api_base_url: str = DEFAULT_API_BASE_URL
    onelake_base_url: str = DEFAULT_ONELAKE_BASE_URL
    fabric_scope: str = DEFAULT_FABRIC_SCOPE
    storage_scope: str = DEFAULT_STORAGE_SCOPE
    sql_scope: str = DEFAULT_SQL_SCOPE
    livy_api_version: str = DEFAULT_LIVY_API_VERSION
    default_degrees_of_parallelism: int = DEFAULT_DEGREES_OF_PARALLELISM


def _first(*values: object) -> object | None:
    """Return the first non-empty value in resolution order."""

    for value in values:
        if value is not None and value != "":
            return value
    return None


def resolve_settings(
    config_settings: FabricSettings | None = None,
    *,
    api_base_url: str | None = None,
    onelake_base_url: str | None = None,
    fabric_scope: str | None = None,
    storage_scope: str | None = None,
    sql_scope: str | None = None,
    livy_api_version: str | None = None,
    default_degrees_of_parallelism: int | None = None,
) -> FabricSettings:
    """Resolve connection settings: CLI override -> config -> env var -> default."""

    base = config_settings or FabricSettings()
    dop = _first(
        default_degrees_of_parallelism,
        base.default_degrees_of_parallelism,
        os.environ.get("FABRIC_DEGREES_OF_PARALLELISM"),
        DEFAULT_DEGREES_OF_PARALLELISM,
    )
    return FabricSettings(
        api_base_url=str(
            _first(
                api_base_url,
                os.environ.get("FABRIC_API_BASE_URL"),
                base.api_base_url,
                DEFAULT_API_BASE_URL,
            )
        ),
        onelake_base_url=str(
            _first(
                onelake_base_url,
                os.environ.get("ONELAKE_BASE_URL"),
                base.onelake_base_url,
                DEFAULT_ONELAKE_BASE_URL,
            )
        ),
        fabric_scope=str(
            _first(
                fabric_scope,
                os.environ.get("FABRIC_API_SCOPE"),
                base.fabric_scope,
                DEFAULT_FABRIC_SCOPE,
            )
        ),
        storage_scope=str(
            _first(
                storage_scope,
                os.environ.get("ONELAKE_SCOPE"),
                base.storage_scope,
                DEFAULT_STORAGE_SCOPE,
            )
        ),
        sql_scope=str(
            _first(
                sql_scope,
                os.environ.get("FABRIC_SQL_SCOPE"),
                base.sql_scope,
                DEFAULT_SQL_SCOPE,
            )
        ),
        livy_api_version=str(
            _first(
                livy_api_version,
                os.environ.get("FABRIC_LIVY_API_VERSION"),
                base.livy_api_version,
                DEFAULT_LIVY_API_VERSION,
            )
        ),
        default_degrees_of_parallelism=int(dop),
    )
