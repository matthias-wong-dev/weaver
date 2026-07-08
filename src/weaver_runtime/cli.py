from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from ._legacy import load_script_module, run_legacy_main
from .capacity import CapacityError, run_capacity_action
from .config import WeaverConfig, WeaverConfigError, load_weaver_config
from .lakehouse import print_json as print_lakehouse_json
from .lakehouse import sync_lakehouse
from .workspace import print_json as print_workspace_json
from .workspace import push_workspace


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args, passthrough = parser.parse_known_args(argv)
    try:
        return int(args.handler(args, passthrough))
    except (WeaverConfigError, CapacityError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="weaver")
    subcommands = parser.add_subparsers(dest="command", required=True)

    fabric = subcommands.add_parser("fabric")
    fabric_subcommands = fabric.add_subparsers(dest="fabric_command", required=True)

    capacity = fabric_subcommands.add_parser("capacity")
    capacity_subcommands = capacity.add_subparsers(dest="capacity_action", required=True)
    for action in ("status", "resume", "suspend"):
        action_parser = capacity_subcommands.add_parser(action)
        add_config_arg(action_parser)
        action_parser.add_argument("--resource-group")
        action_parser.add_argument("--capacity-name")
        action_parser.add_argument("--subscription-id", default=os.environ.get("FABRIC_SUBSCRIPTION_ID"))
        action_parser.set_defaults(handler=handle_capacity)

    lakehouse = fabric_subcommands.add_parser("lakehouse")
    lakehouse_subcommands = lakehouse.add_subparsers(dest="lakehouse_command", required=True)
    lakehouse_sync = lakehouse_subcommands.add_parser("sync")
    add_config_arg(lakehouse_sync)
    lakehouse_sync.add_argument("--repository", action="append", default=[])
    lakehouse_sync.add_argument("--workspace-name")
    lakehouse_sync.add_argument("--workspace-id")
    lakehouse_sync.add_argument("--lakehouse-name")
    lakehouse_sync.add_argument("--lakehouse-id")
    lakehouse_sync.add_argument("--target-root")
    lakehouse_sync.add_argument("--dry-run", action="store_true")
    lakehouse_sync.add_argument("--show-signatures", action="store_true")
    add_fabric_api_args(lakehouse_sync)
    lakehouse_sync.set_defaults(handler=handle_lakehouse_sync)

    workspace = fabric_subcommands.add_parser("workspace")
    workspace_subcommands = workspace.add_subparsers(dest="workspace_command", required=True)
    workspace_push = workspace_subcommands.add_parser("push")
    add_config_arg(workspace_push)
    workspace_push.add_argument("--source", type=Path)
    workspace_push.add_argument("--workspace-name")
    workspace_push.add_argument("--workspace-id")
    workspace_push.add_argument("--item")
    workspace_push.add_argument("--description")
    workspace_push.add_argument("--prune", action="store_true")
    workspace_push.add_argument("--update-metadata", action="store_true")
    workspace_push.add_argument("--dry-run", action="store_true")
    add_fabric_api_args(workspace_push, include_onelake=False)
    workspace_push.set_defaults(handler=handle_workspace_push)

    platform = fabric_subcommands.add_parser("platform")
    platform_subcommands = platform.add_subparsers(dest="platform_command", required=True)
    platform_push = platform_subcommands.add_parser("push")
    add_config_arg(platform_push)
    platform_push.add_argument("--workspace-name")
    platform_push.add_argument("--workspace-id")
    platform_push.add_argument("--lakehouse-name")
    platform_push.add_argument("--lakehouse-id")
    platform_push.add_argument("--dry-run", action="store_true")
    platform_push.add_argument("--show-signatures", action="store_true")
    add_fabric_api_args(platform_push)
    platform_push.set_defaults(handler=handle_platform_push)

    notebook = fabric_subcommands.add_parser("notebook")
    notebook_subcommands = notebook.add_subparsers(dest="notebook_command", required=True)
    notebook_run = notebook_subcommands.add_parser("run")
    add_config_arg(notebook_run)
    notebook_run.add_argument("--name")
    notebook_run.add_argument("--notebook-id")
    notebook_run.add_argument("--workspace-name")
    notebook_run.add_argument("--workspace-id")
    notebook_run.add_argument("--parameter", action="append", default=[])
    notebook_run.add_argument("--no-wait", action="store_true")
    notebook_run.add_argument("--poll-interval", type=float)
    notebook_run.add_argument("--timeout", type=float)
    add_fabric_api_args(notebook_run, include_onelake=False)
    notebook_run.set_defaults(handler=handle_notebook_run)

    spark = fabric_subcommands.add_parser("spark")
    spark_subcommands = spark.add_subparsers(dest="spark_command", required=True)
    spark_run = spark_subcommands.add_parser("run")
    add_config_arg(spark_run)
    spark_run.add_argument("--file", type=Path)
    spark_run.add_argument("--code")
    spark_run.add_argument("--workspace-name")
    spark_run.add_argument("--workspace-id")
    spark_run.add_argument("--lakehouse-name")
    spark_run.add_argument("--lakehouse-id")
    spark_run.set_defaults(handler=handle_spark_run)

    sql = fabric_subcommands.add_parser("sql")
    sql_subcommands = sql.add_subparsers(dest="sql_command", required=True)
    sql_run = sql_subcommands.add_parser("run")
    add_config_arg(sql_run)
    sql_run.add_argument("--server")
    sql_run.add_argument("--database")
    sql_run.add_argument("--sql")
    sql_run.add_argument("--file")
    sql_run.add_argument("--stdin", action="store_true")
    sql_run.add_argument("--show-connection-string", action="store_true")
    sql_run.set_defaults(handler=handle_sql_run)

    build = fabric_subcommands.add_parser("build")
    build_subcommands = build.add_subparsers(dest="build_command", required=True)
    build_ses = build_subcommands.add_parser("ses")
    add_config_arg(build_ses)
    build_ses.add_argument("--source", type=Path)
    build_ses.add_argument("--server")
    build_ses.add_argument("--database")
    build_ses.set_defaults(handler=handle_build_ses)

    return parser


def add_config_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", type=Path, required=True)


def add_fabric_api_args(parser: argparse.ArgumentParser, *, include_onelake: bool = True) -> None:
    sync_folder = load_script_module("sync_folder")
    parser.add_argument("--api-base-url", default=os.environ.get("FABRIC_API_BASE_URL", sync_folder.DEFAULT_API_BASE_URL))
    parser.add_argument("--fabric-scope", default=os.environ.get("FABRIC_API_SCOPE", sync_folder.DEFAULT_FABRIC_SCOPE))
    parser.add_argument("--scope", default=os.environ.get("FABRIC_API_SCOPE", sync_folder.DEFAULT_FABRIC_SCOPE))
    parser.add_argument("--workers", type=int, default=32)
    if include_onelake:
        parser.add_argument("--onelake-base-url", default=os.environ.get("ONELAKE_BASE_URL", sync_folder.DEFAULT_ONELAKE_BASE_URL))
        parser.add_argument("--storage-scope", default=os.environ.get("ONELAKE_SCOPE", sync_folder.DEFAULT_STORAGE_SCOPE))


def load_config(args: argparse.Namespace) -> WeaverConfig:
    return load_weaver_config(args.config)


def handle_capacity(args: argparse.Namespace, passthrough: list[str]) -> int:
    config = load_config(args)
    capacity = config.fabric.capacity
    if capacity is None and (not args.resource_group or not args.capacity_name):
        raise WeaverConfigError("fabric.capacity is required unless both capacity arguments are provided")
    return run_capacity_action(
        args.capacity_action,
        resource_group=args.resource_group or capacity.resource_group,
        capacity_name=args.capacity_name or capacity.name,
        subscription_id=args.subscription_id,
        extra_args=passthrough,
    )


def handle_lakehouse_sync(args: argparse.Namespace, passthrough: list[str]) -> int:
    config = load_config(args)
    payload = sync_lakehouse(config, args)
    print_lakehouse_json(payload)
    return 0


def handle_workspace_push(args: argparse.Namespace, passthrough: list[str]) -> int:
    config = load_config(args)
    payload = push_workspace(config, args)
    print_workspace_json(payload)
    return 0


def handle_platform_push(args: argparse.Namespace, passthrough: list[str]) -> int:
    config = load_config(args)
    lakehouse_payload = sync_lakehouse(config, args)
    workspace_args = argparse.Namespace(
        **{
            **vars(args),
            "source": None,
            "item": None,
            "description": None,
            "prune": False,
            "update_metadata": False,
        }
    )
    workspace_payload = push_workspace(config, workspace_args)
    print_lakehouse_json(
        {
            "config": str(config.path),
            "lakehouse_sync": lakehouse_payload,
            "workspace_push": workspace_payload,
            "success": True,
        }
    )
    return 0


def handle_notebook_run(args: argparse.Namespace, passthrough: list[str]) -> int:
    config = load_config(args)
    workspace_name = args.workspace_name or (
        config.fabric.workspace.name if config.fabric.workspace else None
    )
    argv: list[str] = []
    if args.name:
        argv.extend(["--notebook", args.name])
    if args.notebook_id:
        argv.extend(["--notebook-id", args.notebook_id])
    if args.workspace_id:
        argv.extend(["--workspace-id", args.workspace_id])
    elif workspace_name:
        argv.extend(["--workspace-name", workspace_name])
    for parameter in args.parameter:
        argv.extend(["--parameter", parameter])
    if args.no_wait:
        argv.append("--no-wait")
    if args.poll_interval is not None:
        argv.extend(["--poll-interval", str(args.poll_interval)])
    if args.timeout is not None:
        argv.extend(["--timeout", str(args.timeout)])
    argv.extend(["--api-base-url", args.api_base_url, "--scope", args.scope])
    argv.extend(passthrough)
    return run_legacy_main("run_fabric_notebook_job", argv)


def handle_spark_run(args: argparse.Namespace, passthrough: list[str]) -> int:
    config = load_config(args)
    lakehouse = config.fabric.lakehouse
    workspace_name = args.workspace_name or (lakehouse.workspace if lakehouse else None)
    lakehouse_name = args.lakehouse_name or (lakehouse.name if lakehouse else None)
    argv: list[str] = []
    if args.file:
        argv.extend(["--file", str(args.file)])
    if args.code:
        argv.extend(["--code", args.code])
    if args.workspace_id:
        argv.extend(["--workspace-id", args.workspace_id])
    elif workspace_name:
        argv.extend(["--workspace-name", workspace_name])
    if args.lakehouse_id:
        argv.extend(["--lakehouse-id", args.lakehouse_id])
    elif lakehouse_name:
        argv.extend(["--lakehouse-name", lakehouse_name])
    argv.extend(passthrough)
    return run_legacy_main("sparksession", argv)


def handle_sql_run(args: argparse.Namespace, passthrough: list[str]) -> int:
    config = load_config(args)
    ses = config.fabric.ses
    server = args.server or (ses.server if ses else None)
    database = args.database or (ses.database if ses else None)
    argv = []
    if server:
        argv.extend(["--server", server])
    if database:
        argv.extend(["--database", database])
    if args.sql:
        argv.extend(["--sql", args.sql])
    if args.file:
        argv.extend(["--file", args.file])
    if args.stdin:
        argv.append("--stdin")
    if args.show_connection_string:
        argv.append("--show-connection-string")
    argv.extend(passthrough)
    return run_legacy_main("sqlserver", argv)


def handle_build_ses(args: argparse.Namespace, passthrough: list[str]) -> int:
    config = load_config(args)
    ses = config.fabric.ses
    if ses is None:
        raise WeaverConfigError("fabric.ses is required")
    source = args.source or ses.source
    server = args.server or ses.server
    database = args.database or ses.database
    argv = ["--ses-dir", str(source), "--server", server, "--database", database, *passthrough]
    return run_legacy_main("create_ses_views", argv)


if __name__ == "__main__":
    raise SystemExit(main())
