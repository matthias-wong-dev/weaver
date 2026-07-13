from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from ._legacy import run_legacy_main
from .capacity import CapacityError, run_capacity_action
from .dbrep.cli import add_dbrep_subcommands
from .dbrep.errors import WeaverError
from .errors import CommandError
from .fabric.client import FabricClientError
from .fabric.settings import resolve_settings
from .workspace import print_json as print_workspace_json
from .workspace import WorkspacePushRequest, push_workspace


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args, []))
    except (CommandError, CapacityError, WeaverError, FabricClientError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="weaver")
    subcommands = parser.add_subparsers(dest="command", required=True)

    add_dbrep_subcommands(subcommands)

    fabric = subcommands.add_parser("fabric")
    fabric_subcommands = fabric.add_subparsers(dest="fabric_command", required=True)

    _add_capacity(fabric_subcommands)
    _add_workspace(fabric_subcommands)
    _add_notebook(fabric_subcommands)

    return parser


def add_connection_args(parser: argparse.ArgumentParser) -> None:
    """Add the supported Fabric API override."""
    parser.add_argument("--api-base-url")


def _add_capacity(fabric_subcommands) -> None:
    capacity = fabric_subcommands.add_parser("capacity")
    capacity_subcommands = capacity.add_subparsers(dest="capacity_action", required=True)
    for action in ("status", "resume", "suspend"):
        action_parser = capacity_subcommands.add_parser(action)
        action_parser.add_argument("--resource-group", required=True)
        action_parser.add_argument("--capacity-name", required=True)
        action_parser.add_argument("--subscription-id", default=os.environ.get("FABRIC_SUBSCRIPTION_ID"))
        action_parser.set_defaults(handler=handle_capacity)


def _add_workspace(fabric_subcommands) -> None:
    workspace = fabric_subcommands.add_parser("workspace")
    workspace_subcommands = workspace.add_subparsers(dest="workspace_command", required=True)
    push = workspace_subcommands.add_parser("push")
    push.add_argument("--source", type=Path, required=True)
    push.add_argument("--workspace-name")
    push.add_argument("--workspace-id")
    push.add_argument("--item")
    push.add_argument("--description")
    push.add_argument("--prune", action="store_true")
    push.add_argument("--update-metadata", action="store_true")
    push.add_argument("--dry-run", action="store_true")
    add_connection_args(push)
    push.set_defaults(handler=handle_workspace_push)


def _add_notebook(fabric_subcommands) -> None:
    notebook = fabric_subcommands.add_parser("notebook")
    notebook_subcommands = notebook.add_subparsers(dest="notebook_command", required=True)
    run = notebook_subcommands.add_parser("run")
    run.add_argument("--name")
    run.add_argument("--notebook-id")
    run.add_argument("--workspace-name")
    run.add_argument("--workspace-id")
    run.add_argument("--parameter", action="append", default=[])
    run.add_argument("--no-wait", action="store_true")
    run.add_argument("--poll-interval", type=float)
    run.add_argument("--timeout", type=float)
    add_connection_args(run)
    run.set_defaults(handler=handle_notebook_run)


def handle_capacity(args: argparse.Namespace, passthrough: list[str]) -> int:
    return run_capacity_action(
        args.capacity_action,
        resource_group=args.resource_group,
        capacity_name=args.capacity_name,
        subscription_id=args.subscription_id,
        extra_args=passthrough,
    )


def handle_workspace_push(args: argparse.Namespace, passthrough: list[str]) -> int:
    if not args.workspace_name and not args.workspace_id:
        raise CommandError("provide --workspace-name or --workspace-id")
    settings = resolve_settings(api_base_url=args.api_base_url)
    payload = push_workspace(WorkspacePushRequest(
        source=args.source,
        workspace_name=args.workspace_name,
        workspace_id=args.workspace_id,
        item=args.item,
        description=args.description,
        prune=args.prune,
        update_metadata=args.update_metadata,
        dry_run=args.dry_run,
        api_base_url=settings.api_base_url,
        scope=settings.fabric_scope,
    ))
    print_workspace_json(payload)
    return 0


def handle_notebook_run(args: argparse.Namespace, passthrough: list[str]) -> int:
    if not args.workspace_name and not args.workspace_id:
        raise CommandError("provide --workspace-name or --workspace-id")
    if not args.name and not args.notebook_id:
        raise CommandError("provide --name or --notebook-id")
    settings = resolve_settings(api_base_url=args.api_base_url)
    argv: list[str] = []
    if args.name:
        argv.extend(["--notebook", args.name])
    if args.notebook_id:
        argv.extend(["--notebook-id", args.notebook_id])
    if args.workspace_id:
        argv.extend(["--workspace-id", args.workspace_id])
    else:
        argv.extend(["--workspace-name", args.workspace_name])
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
