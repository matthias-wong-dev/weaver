from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from ._legacy import run_legacy_main
from .capacity import CapacityError, run_capacity_action
from .config import WeaverConfig, WeaverConfigError, load_weaver_config
from .dbrep.cli import add_dbrep_subcommands
from .dbrep.errors import WeaverError
from .fabric.client import FabricClientError
from .fabric.settings import resolve_settings
from .fabric_cli import (
    run_livy_submit,
    run_onelake_sync,
    run_platform_push,
    run_sql_execute,
)
from .workspace import print_json as print_workspace_json
from .workspace import push_workspace


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args, passthrough = parser.parse_known_args(argv)
    try:
        return int(args.handler(args, passthrough))
    except (WeaverConfigError, CapacityError, WeaverError, FabricClientError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="weaver")
    subcommands = parser.add_subparsers(dest="command", required=True)

    add_dbrep_subcommands(subcommands)

    fabric = subcommands.add_parser("fabric")
    fabric_subcommands = fabric.add_subparsers(dest="fabric_command", required=True)

    _add_capacity(fabric_subcommands)
    _add_onelake(fabric_subcommands)
    _add_platform(fabric_subcommands)
    _add_workspace(fabric_subcommands)
    _add_notebook(fabric_subcommands)
    _add_livy(fabric_subcommands)
    _add_sql(fabric_subcommands)

    return parser


def add_config_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", type=Path, required=True)


def add_connection_args(parser: argparse.ArgumentParser, *, include_onelake: bool = True) -> None:
    """Add generic Fabric connection overrides (resolved against env config)."""

    parser.add_argument("--api-base-url")
    if include_onelake:
        parser.add_argument("--onelake-base-url")


def add_lakehouse_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--workspace-name")
    parser.add_argument("--workspace-id")
    parser.add_argument("--lakehouse-name")
    parser.add_argument("--lakehouse-id")


def _bool_flag(parser: argparse.ArgumentParser, name: str, default: bool, help_text: str) -> None:
    dest = name.replace("-", "_")
    group = parser.add_mutually_exclusive_group()
    group.add_argument(f"--{name}", dest=dest, action="store_true", default=default, help=help_text)
    group.add_argument(f"--no-{name}", dest=dest, action="store_false")


def _add_capacity(fabric_subcommands) -> None:
    capacity = fabric_subcommands.add_parser("capacity")
    capacity_subcommands = capacity.add_subparsers(dest="capacity_action", required=True)
    for action in ("status", "resume", "suspend"):
        action_parser = capacity_subcommands.add_parser(action)
        add_config_arg(action_parser)
        action_parser.add_argument("--resource-group")
        action_parser.add_argument("--capacity-name")
        action_parser.add_argument("--subscription-id", default=os.environ.get("FABRIC_SUBSCRIPTION_ID"))
        action_parser.set_defaults(handler=handle_capacity)


def _add_onelake(fabric_subcommands) -> None:
    onelake = fabric_subcommands.add_parser("onelake")
    onelake_subcommands = onelake.add_subparsers(dest="onelake_command", required=True)
    sync = onelake_subcommands.add_parser("sync")
    add_config_arg(sync)
    sync.add_argument("--source", type=Path, required=True)
    sync.add_argument("--target-folder", required=True)
    add_lakehouse_args(sync)
    _bool_flag(sync, "respect-ignore", True, "honour .weaverignore/.gitignore (default)")
    _bool_flag(sync, "signatures", True, "skip unchanged files via signatures (default)")
    _bool_flag(sync, "delete", True, "delete remote files missing locally, scoped to target folder")
    sync.add_argument("--degrees-of-parallelism", type=int)
    sync.add_argument("--dry-run", action="store_true")
    add_connection_args(sync)
    sync.set_defaults(handler=handle_onelake_sync)


def _add_platform(fabric_subcommands) -> None:
    platform = fabric_subcommands.add_parser("platform")
    platform_subcommands = platform.add_subparsers(dest="platform_command", required=True)
    push = platform_subcommands.add_parser("push")
    add_config_arg(push)
    push.add_argument("--name", action="append", default=[])
    add_lakehouse_args(push)
    push.add_argument("--degrees-of-parallelism", type=int)
    push.add_argument("--dry-run", action="store_true")
    add_connection_args(push)
    push.set_defaults(handler=handle_platform_push)


def _add_workspace(fabric_subcommands) -> None:
    workspace = fabric_subcommands.add_parser("workspace")
    workspace_subcommands = workspace.add_subparsers(dest="workspace_command", required=True)
    push = workspace_subcommands.add_parser("push")
    add_config_arg(push)
    push.add_argument("--source", type=Path)
    push.add_argument("--workspace-name")
    push.add_argument("--workspace-id")
    push.add_argument("--item")
    push.add_argument("--description")
    push.add_argument("--prune", action="store_true")
    push.add_argument("--update-metadata", action="store_true")
    push.add_argument("--dry-run", action="store_true")
    add_connection_args(push, include_onelake=False)
    push.set_defaults(handler=handle_workspace_push)


def _add_notebook(fabric_subcommands) -> None:
    notebook = fabric_subcommands.add_parser("notebook")
    notebook_subcommands = notebook.add_subparsers(dest="notebook_command", required=True)
    run = notebook_subcommands.add_parser("run")
    add_config_arg(run)
    run.add_argument("--name")
    run.add_argument("--notebook-id")
    run.add_argument("--workspace-name")
    run.add_argument("--workspace-id")
    run.add_argument("--parameter", action="append", default=[])
    run.add_argument("--no-wait", action="store_true")
    run.add_argument("--poll-interval", type=float)
    run.add_argument("--timeout", type=float)
    add_connection_args(run, include_onelake=False)
    run.set_defaults(handler=handle_notebook_run)


def _add_livy(fabric_subcommands) -> None:
    livy = fabric_subcommands.add_parser("livy")
    livy_subcommands = livy.add_subparsers(dest="livy_command", required=True)
    submit = livy_subcommands.add_parser("submit")
    add_config_arg(submit)
    submit.add_argument("--kind", choices=["pyspark", "sql"], default="pyspark")
    submit.add_argument("--file")
    submit.add_argument("--code")
    add_lakehouse_args(submit)
    submit.add_argument("--api-version")
    submit.add_argument("--poll-interval", type=float, default=5.0)
    submit.add_argument("--timeout", type=float, default=1200.0)
    add_connection_args(submit, include_onelake=False)
    submit.set_defaults(handler=handle_livy_submit)


def _add_sql(fabric_subcommands) -> None:
    sql = fabric_subcommands.add_parser("sql")
    sql_subcommands = sql.add_subparsers(dest="sql_command", required=True)
    execute = sql_subcommands.add_parser("execute")
    add_config_arg(execute)
    execute.add_argument("--server")
    execute.add_argument("--database")
    execute.add_argument("--sql")
    execute.add_argument("--file")
    execute.add_argument("--stdin", action="store_true")
    execute.add_argument("--show-connection-string", action="store_true")
    execute.set_defaults(handler=handle_sql_execute)


def load_config(args: argparse.Namespace) -> WeaverConfig:
    return load_weaver_config(args.config)


def _emit(payload: dict) -> int:
    print(json.dumps(payload, indent=2), flush=True)
    return 0


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


def handle_onelake_sync(args: argparse.Namespace, passthrough: list[str]) -> int:
    return _emit(run_onelake_sync(load_config(args), args))


def handle_platform_push(args: argparse.Namespace, passthrough: list[str]) -> int:
    return _emit(run_platform_push(load_config(args), args))


def handle_livy_submit(args: argparse.Namespace, passthrough: list[str]) -> int:
    return _emit(run_livy_submit(load_config(args), args))


def handle_sql_execute(args: argparse.Namespace, passthrough: list[str]) -> int:
    return _emit(run_sql_execute(load_config(args), args))


def handle_workspace_push(args: argparse.Namespace, passthrough: list[str]) -> int:
    config = load_config(args)
    settings = resolve_settings(config.fabric.settings, api_base_url=args.api_base_url)
    args.api_base_url = settings.api_base_url
    args.scope = settings.fabric_scope
    payload = push_workspace(config, args)
    print_workspace_json(payload)
    return 0


def handle_notebook_run(args: argparse.Namespace, passthrough: list[str]) -> int:
    config = load_config(args)
    settings = resolve_settings(config.fabric.settings, api_base_url=args.api_base_url)
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
    argv.extend(["--api-base-url", settings.api_base_url, "--scope", settings.fabric_scope])
    argv.extend(passthrough)
    return run_legacy_main("run_fabric_notebook_job", argv)


if __name__ == "__main__":
    raise SystemExit(main())
