"""Argparse wiring for the database-representation subcommands."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .commands import run_build, run_generate, run_load, run_wipe


def add_dbrep_subcommands(subcommands: argparse._SubParsersAction) -> None:
    """Register the generate/build/load/wipe lifecycle on the top-level parser."""

    generate = subcommands.add_parser(
        "generate", help="generate deployment/runtime artifacts without applying them"
    )
    _config_arg(generate)
    generate.add_argument("--from", dest="from_aliases", required=True)
    generate.add_argument("--to", dest="to_aliases", required=True)
    generate.add_argument("--out", type=Path)
    generate.add_argument("--prune", action="store_true")
    generate.add_argument("--strict", action="store_true")
    generate.set_defaults(handler=_handle_generate)

    build = subcommands.add_parser("build", help="build database representations")
    _config_arg(build)
    build.add_argument("--from", dest="from_aliases", required=True)
    build.add_argument("--to", dest="to_aliases", required=True)
    build.add_argument("--prune", action="store_true")
    build.add_argument("--dry-run", action="store_true")
    build.add_argument("--strict", action="store_true")
    build.add_argument("--assume-installed-runtime", action="store_true")
    build.set_defaults(handler=_handle_build)

    load = subcommands.add_parser("load", help="target-only load from installed runtime")
    _config_arg(load)
    load.add_argument("--target", required=True)
    load.add_argument("--object", dest="objects", action="append", default=[])
    load.add_argument("--include-static", action="store_true")
    load.add_argument("--dry-run", action="store_true")
    load.add_argument("--strict", action="store_true", default=True)
    load.add_argument("--no-strict", dest="strict", action="store_false")
    load.set_defaults(handler=_handle_load)

    wipe = subcommands.add_parser("wipe", help="wipe a SQL/Files/Delta target's objects or materialisations")
    _config_arg(wipe)
    wipe.add_argument("--target", required=True)
    wipe.set_defaults(handler=_handle_wipe)


def _config_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", type=Path, required=True)


def _emit(payload: dict) -> int:
    print(json.dumps(payload, indent=2))
    return 0


def _handle_build(args: argparse.Namespace, passthrough: list) -> int:
    return _emit(
        run_build(
            args.config,
            args.from_aliases,
            args.to_aliases,
            prune=args.prune,
            dry_run=args.dry_run,
            strict=args.strict,
            assume_installed_runtime=args.assume_installed_runtime,
        )
    )


def _handle_load(args: argparse.Namespace, passthrough: list) -> int:
    return _emit(
        run_load(
            args.config,
            args.target,
            objects=tuple(args.objects) or None,
            include_static=args.include_static,
            dry_run=args.dry_run,
            strict=args.strict,
        )
    )


def _handle_generate(args: argparse.Namespace, passthrough: list) -> int:
    return _emit(
        run_generate(
            args.config,
            args.from_aliases,
            args.to_aliases,
            out=args.out,
            prune=args.prune,
            strict=args.strict,
        )
    )


def _handle_wipe(args: argparse.Namespace, passthrough: list) -> int:
    return _emit(run_wipe(args.config, args.target))
