from __future__ import annotations

import os

import pytest


@pytest.fixture(scope="session")
def _fabric_workspace():
    """Resolve the workspace to provision test items in, or skip.

    Opt in by setting the workspace to create disposable test items in (auth uses
    ``az login`` / DefaultAzureCredential)::

        WEAVER_FABRIC_WORKSPACE=<workspace name or id>
        WEAVER_FABRIC_SQL_DOP=4   # optional, default 4

    The fixtures create a warehouse and a lakehouse on demand and delete them at
    the end of the session.
    """

    workspace = os.environ.get("WEAVER_FABRIC_WORKSPACE")
    if not workspace:
        pytest.skip("set WEAVER_FABRIC_WORKSPACE to run Fabric integration tests")

    from fabric_api import fabric_token, resolve_workspace

    token = fabric_token()
    workspace_id, workspace_name = resolve_workspace(token, workspace)
    return {"id": workspace_id, "name": workspace_name}


@pytest.fixture(scope="session")
def fabric_sql_target(_fabric_workspace):
    """Create a disposable Fabric Warehouse for the session; delete it after."""

    from fabric_api import create_warehouse, delete_item, fabric_token, unique_name
    from fabric_helpers import wait_queryable

    workspace_id = _fabric_workspace["id"]
    warehouse = create_warehouse(fabric_token(), workspace_id, unique_name("weaver_pytest_wh"))
    wait_queryable(warehouse["connection_string"], warehouse["name"])
    target = {
        "server": warehouse["connection_string"],
        "database": warehouse["name"],
        "dop": int(os.environ.get("WEAVER_FABRIC_SQL_DOP", "4")),
    }
    try:
        yield target
    finally:
        delete_item(fabric_token(), workspace_id, "warehouses", warehouse["id"])


@pytest.fixture()
def clean_fabric_sql(fabric_sql_target):
    """Reset managed test objects before and after each Fabric SQL test."""

    from fabric_helpers import reset

    reset(fabric_sql_target["server"], fabric_sql_target["database"])
    try:
        yield fabric_sql_target
    finally:
        reset(fabric_sql_target["server"], fabric_sql_target["database"])


@pytest.fixture(scope="session")
def fabric_lakehouse_target(_fabric_workspace):
    """Create a disposable Fabric Lakehouse for the session; delete it after."""

    from fabric_api import create_lakehouse, delete_item, fabric_token, unique_name

    workspace_id = _fabric_workspace["id"]
    lakehouse = create_lakehouse(fabric_token(), workspace_id, unique_name("weaver_pytest_lh"))
    try:
        yield {"workspace": _fabric_workspace["name"], "lakehouse": lakehouse["name"]}
    finally:
        delete_item(fabric_token(), workspace_id, "lakehouses", lakehouse["id"])
