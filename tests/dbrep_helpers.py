"""Shared helpers for database-representation tests.

These build small, generic SES source trees and configs on disk. Object names
are generic (T0/T1/... , Raw.Drop, Stage.Record). Objects use plain classes with
static ``self.repo[...]`` references so build-time (static) discovery works
without importing PySpark.
"""

from __future__ import annotations

from pathlib import Path

from weaver_runtime.dbrep.config import (
    load_databases_config,
    parse_databases_config,
    parse_environment_config,
    resolve_database,
)


def _docstring(lines: list[str]) -> str:
    body = "\n".join(lines)
    return f'"""\n{body}\n"""\n'


def write_python_table(
    folder: Path,
    schema: str,
    obj: str,
    *,
    primary_key: str | None = "record_id",
    auto_delete: bool = False,
    static: bool = False,
    deps: tuple[str, ...] = (),
    schema_cols: tuple[tuple[str, str], ...] = (),
) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    meta = [f"Table ID: {schema}.{obj}", f"Description: {obj} table.", f"Lineage: Builds {obj}."]
    if primary_key:
        meta.append(f"Primary key: {primary_key}")
    if auto_delete:
        meta.append("Auto delete: true")
    if static:
        meta.append("Static: true")
    if schema_cols:
        meta.append("Schema:")
        for column, column_type in schema_cols:
            meta.append(f"  {column}: {column_type}")

    dep_lines = "\n".join(f'        _{i} = self.repo["{ref}"]' for i, ref in enumerate(deps))
    if not dep_lines:
        dep_lines = "        pass"
    source = _docstring(meta) + (
        "\n\nfrom weaver_runtime.dbrep.objects import Table\n\n\n"
        f"class {schema}__{obj}(Table):\n"
        f"    def read(self, spark):\n"
        f"{dep_lines}\n"
        f"        return None\n"
    )
    path = folder / f"{schema}__{obj}.py"
    path.write_text(source, encoding="utf-8")
    return path


def write_python_folder(
    folder: Path,
    schema: str,
    obj: str,
    *,
    deps: tuple[str, ...] = (),
) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    meta = [f"Folder ID: {schema}.{obj}", f"Description: {obj} folder.", f"Lineage: Writes {obj}."]
    dep_lines = "\n".join(f'        _{i} = self.repo["{ref}"]' for i, ref in enumerate(deps))
    if not dep_lines:
        dep_lines = "        pass"
    source = _docstring(meta) + (
        "\n\nfrom weaver_runtime.dbrep.objects import Folder\n\n\n"
        f"class {schema}__{obj}(Folder):\n"
        f"    def load(self):\n"
        f"{dep_lines}\n"
        f"        return None\n"
    )
    path = folder / f"{schema}__{obj}.py"
    path.write_text(source, encoding="utf-8")
    return path


def write_sql_table(
    folder: Path,
    schema: str,
    obj: str,
    *,
    primary_key: str | None = "record_id",
    query: str = "select 1 as record_id",
) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    meta = [f"Table ID: {schema}.{obj}", f"Description: {obj} table.", f"Lineage: Builds {obj}."]
    if primary_key:
        meta.append(f"Primary key: {primary_key}")
    source = "/*\n" + "\n".join(meta) + "\n*/\n" + query + "\n"
    path = folder / f"{schema}.{obj}.sql"
    path.write_text(source, encoding="utf-8")
    return path


def make_config(
    tmp_path: Path,
    servers: dict,
    databases: dict,
    *,
    base_dir: Path | None = None,
):
    """Build a DatabasesConfig from in-memory server/database mappings."""

    base = base_dir or tmp_path
    environment = parse_environment_config(
        {"version": 1, "servers": servers}, base_dir=base
    )
    return parse_databases_config(
        {"version": 1, "databases": databases}, environment, base_dir=base
    )


def resolve(config, alias):
    return resolve_database(config.get(alias), config.environment)


def write_config_files(tmp_path: Path, servers: dict, databases: dict) -> Path:
    """Write env.yml and weaver.yml to disk and return the weaver.yml path."""

    import yaml

    (tmp_path / "env.yml").write_text(
        yaml.safe_dump({"version": 1, "servers": servers}, sort_keys=False),
        encoding="utf-8",
    )
    weaver = {
        "version": 1,
        "uses": {"environment": "env.yml"},
        "databases": databases,
    }
    weaver_path = tmp_path / "weaver.yml"
    weaver_path.write_text(yaml.safe_dump(weaver, sort_keys=False), encoding="utf-8")
    return weaver_path


def load_config(weaver_path: Path):
    return load_databases_config(weaver_path)
