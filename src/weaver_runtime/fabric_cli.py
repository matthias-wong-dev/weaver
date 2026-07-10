"""CLI glue between Weaver config/args and the shared Fabric substrate.

Each handler resolves connection settings (CLI override -> env config ->
default), resolves the target, invokes the substrate, and returns a plain dict
that the CLI serialises as JSON.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from .config import PlatformConfig, WeaverConfig, WeaverConfigError
from .fabric import livy as fabric_livy
from .fabric import onelake as fabric_onelake
from .fabric import sql as fabric_sql
from .fabric import sync as fabric_sync
from .fabric.context import resolve_lakehouse_target
from .fabric.ignore import default_platform_ignore_spec
from .fabric.settings import FabricSettings, resolve_settings


def _resolve_settings(config: WeaverConfig, args: argparse.Namespace) -> FabricSettings:
    return resolve_settings(
        config.fabric.settings,
        api_base_url=getattr(args, "api_base_url", None),
        onelake_base_url=getattr(args, "onelake_base_url", None),
        livy_api_version=getattr(args, "api_version", None),
        default_degrees_of_parallelism=getattr(args, "degrees_of_parallelism", None),
    )


def _lakehouse_coordinates(
    config: WeaverConfig, args: argparse.Namespace
) -> tuple[str | None, str | None]:
    """Return (workspace_name, lakehouse_name) from args then config defaults."""

    lakehouse = config.fabric.lakehouse
    workspace_name = getattr(args, "workspace_name", None) or (
        (lakehouse.workspace if lakehouse else None)
        or (config.fabric.workspace.name if config.fabric.workspace else None)
    )
    lakehouse_name = getattr(args, "lakehouse_name", None) or (
        lakehouse.name if lakehouse else None
    )
    return workspace_name, lakehouse_name


def run_onelake_sync(config: WeaverConfig, args: argparse.Namespace) -> dict[str, Any]:
    """Sync one local folder to one Lakehouse Files folder."""

    settings = _resolve_settings(config, args)
    source = Path(args.source)
    dop = args.degrees_of_parallelism or settings.default_degrees_of_parallelism

    if args.dry_run:
        return fabric_sync.sync_folder(
            _dry_run_target(settings),
            source,
            args.target_folder,
            respect_ignore=args.respect_ignore,
            signatures=args.signatures,
            delete=args.delete,
            degrees_of_parallelism=dop,
            dry_run=True,
        )

    workspace_name, lakehouse_name = _lakehouse_coordinates(config, args)
    if not args.workspace_id and not workspace_name:
        raise WeaverConfigError("provide --workspace-id/--workspace-name or configure fabric.lakehouse")
    target = resolve_lakehouse_target(
        settings,
        workspace_id=args.workspace_id,
        workspace_name=workspace_name,
        lakehouse_id=args.lakehouse_id,
        lakehouse_name=lakehouse_name,
    )
    payload = fabric_sync.sync_folder(
        target,
        source,
        args.target_folder,
        respect_ignore=args.respect_ignore,
        signatures=args.signatures,
        delete=args.delete,
        degrees_of_parallelism=dop,
    )
    payload["workspace"] = target.workspace_name
    payload["lakehouse"] = target.lakehouse_name
    return payload


def _selected_platform_sources(platform: PlatformConfig, names: list[str]):
    if not names:
        return list(platform.sources)
    by_name = {source.name: source for source in platform.sources}
    missing = [name for name in names if name not in by_name]
    if missing:
        raise WeaverConfigError(f"unknown platform sources: {sorted(missing)}")
    return [by_name[name] for name in names]


def run_platform_push(config: WeaverConfig, args: argparse.Namespace) -> dict[str, Any]:
    """Sync the declared platform source folders into OneLake."""

    if config.fabric.platform is None:
        raise WeaverConfigError("fabric.platform is required for platform push")

    settings = _resolve_settings(config, args)
    platform = config.fabric.platform
    sources = _selected_platform_sources(platform, list(getattr(args, "name", []) or []))
    dop = args.degrees_of_parallelism or settings.default_degrees_of_parallelism
    baseline = default_platform_ignore_spec()

    def target_folder(source) -> str:
        return f"{platform.target_root}/{source.target}"

    if args.dry_run:
        dry_target = _dry_run_target(settings)
        results = [
            fabric_sync.sync_folder(
                dry_target,
                source.source,
                target_folder(source),
                respect_ignore=source.respect_ignore,
                signatures=True,
                delete=True,
                degrees_of_parallelism=dop,
                dry_run=True,
                extra_ignore=baseline,
            )
            for source in sources
        ]
        return {
            "operation": "fabric.platform.push",
            "config": str(config.path),
            "target_root": f"Files/{platform.target_root}",
            "planned_targets": [f"Files/{target_folder(source)}" for source in sources],
            "results": results,
            "dry_run": True,
            "success": True,
        }

    workspace_name, lakehouse_name = _lakehouse_coordinates(config, args)
    target = resolve_lakehouse_target(
        settings,
        workspace_id=args.workspace_id,
        workspace_name=workspace_name,
        lakehouse_id=args.lakehouse_id,
        lakehouse_name=lakehouse_name,
    )
    results = [
        {
            "name": source.name,
            **fabric_sync.sync_folder(
                target,
                source.source,
                target_folder(source),
                respect_ignore=source.respect_ignore,
                signatures=True,
                delete=True,
                degrees_of_parallelism=dop,
                extra_ignore=baseline,
            ),
        }
        for source in sources
    ]
    return {
        "operation": "fabric.platform.push",
        "config": str(config.path),
        "workspace": target.workspace_name,
        "lakehouse": target.lakehouse_name,
        "target_root": f"Files/{platform.target_root}",
        "results": results,
        "success": True,
    }


def run_livy_submit(config: WeaverConfig, args: argparse.Namespace) -> dict[str, Any]:
    """Submit one statement to a Fabric Spark Livy session."""

    if args.file and args.code:
        raise WeaverConfigError("provide only one of --file or --code")
    if args.file:
        code = Path(args.file).read_text(encoding="utf-8")
    elif args.code is not None:
        code = args.code
    else:
        raise WeaverConfigError("provide --file or --code")

    settings = _resolve_settings(config, args)
    workspace_name, lakehouse_name = _lakehouse_coordinates(config, args)
    target = resolve_lakehouse_target(
        settings,
        workspace_id=args.workspace_id,
        workspace_name=workspace_name,
        lakehouse_id=args.lakehouse_id,
        lakehouse_name=lakehouse_name,
    )
    token = _fabric_token(settings)
    output = fabric_livy.run_code(
        target.workspace_id,
        target.lakehouse_id,
        token,
        code,
        kind=args.kind,
        api_base_url=settings.api_base_url,
        api_version=settings.livy_api_version,
        poll_interval=args.poll_interval,
        timeout=args.timeout,
    )
    return {
        "operation": "fabric.livy.submit",
        "workspace": target.workspace_name,
        "lakehouse": target.lakehouse_name,
        "kind": args.kind,
        "output": _livy_text(output),
        "success": True,
    }


def run_sql_execute(config: WeaverConfig, args: argparse.Namespace) -> dict[str, Any]:
    """Execute SQL against a Fabric Warehouse / SQL endpoint."""

    ses = config.fabric.ses
    server = args.server or (ses.server if ses else None)
    database = args.database or (ses.database if ses else None)
    if not server:
        raise WeaverConfigError("provide --server or configure fabric.ses.server")

    if args.show_connection_string:
        print(fabric_sql.build_connection_string(server, database), flush=True)

    sql_text = _read_sql(args)
    settings = _resolve_settings(config, args)
    result = fabric_sql.execute(sql_text, server, database, sql_scope=settings.sql_scope)
    return {
        "operation": "fabric.sql.execute",
        "server": server,
        "database": database,
        "columns": result.columns,
        "rows": [[_json_safe(value) for value in row] for row in result.rows],
        "rowcount": result.rowcount,
        "success": True,
    }


def _json_safe(value: Any) -> Any:
    """Coerce SQL cell values (datetime, Decimal, bytes, ...) to JSON-native types."""

    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (bytes, bytearray)):
        return value.hex()
    return str(value)


def _read_sql(args: argparse.Namespace) -> str:
    if args.stdin or args.file == "-":
        return sys.stdin.read()
    if args.file:
        return Path(args.file).read_text(encoding="utf-8")
    if args.sql:
        return args.sql
    raise WeaverConfigError("provide --sql, --file, or --stdin")


def _dry_run_target(settings: FabricSettings) -> fabric_onelake.LakehouseTarget:
    return fabric_onelake.LakehouseTarget(
        workspace_id="dry-run",
        lakehouse_id="dry-run",
        storage_token="dry-run",
        onelake_base_url=settings.onelake_base_url,
    )


def _fabric_token(settings: FabricSettings) -> str:
    from .fabric import auth

    return auth.get_token(settings.fabric_scope)


def _livy_text(output: dict[str, Any]) -> Any:
    data = output.get("data") or {}
    if "text/plain" in data:
        return data["text/plain"]
    if "application/json" in data:
        return data["application/json"]
    return output
