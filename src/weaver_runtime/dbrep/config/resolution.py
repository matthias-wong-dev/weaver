"""Resolve database representations to hosts, paths, and materialisations.

The conceptual stack is ``server.database.type.schema.object``:

* ``server``   fourth-level host (filesystem parent, Lakehouse host, or SQL endpoint)
* ``database`` third-level representation name
* ``type``     representation type
* ``schema``   schema / namespace / subfolder
* ``object``   table / view / folder / load object

Host-relative *materialisation* strings (``Files/<db>/<schema>/<object>``;
``Tables/<db>/<schema>/<object>`` locally, ``Tables/<schema>/<object>`` on a
Fabric Lakehouse where the Lakehouse is the database host) are pure and go into
the manifest. Schema and object are always separate path components. Absolute
filesystem paths additionally resolve the host against a base directory and are
only meaningful for local/SES hosts.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .databases import DELTA, FILES, SES, SQL, DatabaseConfig, DatabasesConfig
from .environment import EnvironmentConfig

RUNTIME_RELATIVE_ROOT = "Files/_weaver/runtime"
MANIFEST_RELATIVE_PATH = "Files/_weaver/runtime/manifest.json"
CATALOGUE_RELATIVE_PATH = "Files/_weaver/runtime/catalogue.json"
LOAD_DEPENDENCY_RELATIVE_PATH = "Files/_weaver/runtime/load_dependency.json"


@dataclass(frozen=True)
class SqlIdentity:
    """Fully qualified SQL object identity."""

    server: str
    database: str
    schema: str
    object: str


@dataclass(frozen=True)
class ResolvedDatabase:
    """A database representation bound to its host and base directory."""

    alias: str
    type: str
    database: str
    server_alias: str
    host: str
    degrees_of_parallelism: int | None
    base_dir: Path
    platform: str = "local"

    @property
    def is_fabric(self) -> bool:
        return self.platform == "fabric"

    @property
    def fabric_workspace(self) -> str:
        """Workspace name parsed from a ``Workspace/Lakehouse`` Fabric host."""

        return self.host.split("/", 1)[0]

    @property
    def fabric_lakehouse(self) -> str:
        """Lakehouse name parsed from a ``Workspace/Lakehouse`` Fabric host."""

        parts = self.host.split("/", 1)
        return parts[1] if len(parts) == 2 else self.database

    @property
    def is_ses(self) -> bool:
        return self.type == SES

    @property
    def is_files(self) -> bool:
        return self.type == FILES

    @property
    def is_delta(self) -> bool:
        return self.type == DELTA

    @property
    def is_sql(self) -> bool:
        return self.type == SQL

    @property
    def is_lakehouse(self) -> bool:
        return self.type in (FILES, DELTA)


def resolve_database(
    database: DatabaseConfig,
    environment: EnvironmentConfig,
    base_dir: Path | None = None,
) -> ResolvedDatabase:
    """Bind a database representation to its host server."""

    server = environment.get(database.server)
    return ResolvedDatabase(
        alias=database.alias,
        type=database.type,
        database=database.database,
        server_alias=database.server,
        host=server.server,
        degrees_of_parallelism=server.degrees_of_parallelism,
        base_dir=Path(base_dir) if base_dir is not None else environment.base_dir,
        platform=server.platform,
    )


def resolve_all(config: DatabasesConfig) -> dict[str, ResolvedDatabase]:
    """Resolve every database representation in a config."""

    return {
        database.alias: resolve_database(database, config.environment, config.environment.base_dir)
        for database in config.databases
    }


# --- Absolute filesystem locations (local/SES hosts) -----------------------


def filesystem_host(resolved: ResolvedDatabase) -> Path:
    """Resolve the host to an absolute filesystem path.

    Meaningful for SES and local Lakehouse hosts. For remote hosts (a Fabric
    Lakehouse name or a SQL endpoint) the return value is not a real path and
    must not be used by filesystem adapters.
    """

    path = Path(resolved.host).expanduser()
    if not path.is_absolute():
        path = resolved.base_dir / path
    return path


def ses_source_root(resolved: ResolvedDatabase) -> Path:
    """SES database folder: ``<host>/<database>``."""

    return filesystem_host(resolved) / resolved.database


def lakehouse_root(resolved: ResolvedDatabase) -> Path:
    """Lakehouse host root."""

    return filesystem_host(resolved)


def files_root(resolved: ResolvedDatabase) -> Path:
    return lakehouse_root(resolved) / "Files"


def tables_root(resolved: ResolvedDatabase) -> Path:
    return lakehouse_root(resolved) / "Tables"


def runtime_root(resolved: ResolvedDatabase) -> Path:
    """Installed runtime bundle root under the Lakehouse host."""

    return lakehouse_root(resolved) / "Files" / "_weaver" / "runtime"


def files_object_path(resolved: ResolvedDatabase, schema: str, object_name: str) -> Path:
    return files_root(resolved) / resolved.database / schema / object_name


def delta_table_path(resolved: ResolvedDatabase, schema: str, object_name: str) -> Path:
    root = tables_root(resolved)
    if resolved.is_fabric:
        return root / schema / object_name
    return root / resolved.database / schema / object_name


# --- Host-relative materialisation strings (manifest) ----------------------


def files_materialisation(database: str, schema: str, object_name: str) -> str:
    return f"Files/{database}/{schema}/{object_name}"


def delta_materialisation(
    database: str, schema: str, object_name: str, *, fabric: bool = False
) -> str:
    """Host-relative Delta table path with schema and object as separate dirs.

    Local Lakehouse hosts co-locate multiple databases, so the database is a path
    component: ``Tables/<database>/<schema>/<object>``. A Fabric Lakehouse *is*
    the database host, so the database is omitted: ``Tables/<schema>/<object>``.
    Schema and object are never joined into one dotted directory name.
    """

    if fabric:
        return f"Tables/{schema}/{object_name}"
    return f"Tables/{database}/{schema}/{object_name}"


def sql_identity(resolved: ResolvedDatabase, schema: str, object_name: str) -> SqlIdentity:
    return SqlIdentity(
        server=resolved.host,
        database=resolved.database,
        schema=schema,
        object=object_name,
    )
