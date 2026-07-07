#!/usr/bin/env python3
"""Run the Push to public store notebook, optionally refreshing selected DWG views."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


NOTEBOOK_NAME = "Push to public store"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--view-name",
        action="append",
        default=[],
        help=(
            "DWG view to refresh. Repeat for multiple views. "
            "Use either DWG.ViewName or ViewName."
        ),
    )
    parser.add_argument(
        "--runner",
        default=str(Path(__file__).with_name("run_fabric_notebook_job.py")),
        help="Path to the generic Fabric notebook runner.",
    )
    parser.add_argument(
        "--no-wait",
        action="store_true",
        help="Start the Fabric job and return without polling for completion.",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=None,
        help="Polling interval passed to the generic runner.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=None,
        help="Timeout passed to the generic runner.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    view_names = [name.strip() for name in args.view_name if name.strip()]

    command = [sys.executable, args.runner, NOTEBOOK_NAME]

    if view_names:
        command.extend(["--parameter", f"view_names={','.join(dict.fromkeys(view_names))}"])

    if args.no_wait:
        command.append("--no-wait")
    if args.poll_interval is not None:
        command.extend(["--poll-interval", str(args.poll_interval)])
    if args.timeout is not None:
        command.extend(["--timeout", str(args.timeout)])

    return subprocess.call(command)


if __name__ == "__main__":
    raise SystemExit(main())
