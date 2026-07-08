from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Sequence


class CapacityError(RuntimeError):
    """Raised when Fabric capacity control cannot be invoked."""


ACTION_TO_AZ_VERB = {
    "status": "show",
    "resume": "resume",
    "suspend": "suspend",
}


def capacity_az_args(
    action: str,
    *,
    resource_group: str,
    capacity_name: str,
    subscription_id: str | None = None,
    extra_args: Sequence[str] = (),
) -> list[str]:
    """Build the Azure CLI command for one generic Fabric capacity action."""

    verb = ACTION_TO_AZ_VERB.get(action)
    if verb is None:
        raise CapacityError(f"unknown capacity action: {action}")
    if not resource_group:
        raise CapacityError("capacity resource group is required")
    if not capacity_name:
        raise CapacityError("capacity name is required")

    command = [
        "az",
        "fabric",
        "capacity",
        verb,
        "--resource-group",
        resource_group,
        "--capacity-name",
        capacity_name,
    ]
    if subscription_id:
        command.extend(["--subscription", subscription_id])
    command.extend(extra_args)
    return command


def run_capacity_action(
    action: str,
    *,
    resource_group: str,
    capacity_name: str,
    subscription_id: str | None = None,
    extra_args: Sequence[str] = (),
) -> int:
    """Run a Fabric capacity action through the Azure CLI."""

    if shutil.which("az") is None:
        raise CapacityError("Azure CLI is not installed. Install it with: brew install azure-cli")

    command = capacity_az_args(
        action,
        resource_group=resource_group,
        capacity_name=capacity_name,
        subscription_id=subscription_id or os.environ.get("FABRIC_SUBSCRIPTION_ID"),
        extra_args=extra_args,
    )
    return subprocess.run(command, check=False).returncode
