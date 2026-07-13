from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from weaver_runtime.capacity import capacity_az_args  # noqa: E402
from weaver_runtime.workspace import discover_workspace_sources  # noqa: E402


def test_capacity_az_args_are_explicit() -> None:
    assert capacity_az_args(
        "suspend",
        resource_group="rg-test",
        capacity_name="cap-test",
        subscription_id="sub-test",
    ) == [
        "az",
        "fabric",
        "capacity",
        "suspend",
        "--resource-group",
        "rg-test",
        "--capacity-name",
        "cap-test",
        "--subscription",
        "sub-test",
    ]


def test_workspace_discovery_is_flat_and_ignores_unsupported_files(tmp_path: Path) -> None:
    source = tmp_path / "workspace"
    source.mkdir()
    (source / "Load.ipynb").write_text("{}", encoding="utf-8")
    (source / "Publish.py").write_text("print('ok')\n", encoding="utf-8")
    (source / ".hidden.ipynb").write_text("{}", encoding="utf-8")
    (source / "notes.md").write_text("ignore\n", encoding="utf-8")
    nested = source / "nested"
    nested.mkdir()
    (nested / "Nested.ipynb").write_text("{}", encoding="utf-8")

    assert [item.name for item in discover_workspace_sources(source)] == ["Load", "Publish"]
    assert [item.name for item in discover_workspace_sources(source, item_name="Publish")] == ["Publish"]
