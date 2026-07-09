from __future__ import annotations

import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from weaver_runtime.capacity import capacity_az_args  # noqa: E402
from weaver_runtime.config import WeaverConfigError, load_weaver_config  # noqa: E402
from weaver_runtime.workspace import discover_workspace_sources  # noqa: E402


def write_config(path: Path) -> None:
    path.write_text(
        """
version: 1
fabric:
  capacity:
    resource_group: rg-test
    name: cap-test
  workspace:
    name: Test Workspace
    source: workspace/Test Workspace
  lakehouse:
    workspace: Test Workspace
    name: TestLakehouse
    target_root: Files/platform
    repositories:
      - name: app
        source: ../app
        target: app
  ses:
    source: SES
    server: sql.example.test
    database: Warehouse
""",
        encoding="utf-8",
    )


def test_config_resolves_paths_relative_to_config_file(tmp_path: Path) -> None:
    config_path = tmp_path / "etl" / "weaver.yaml"
    config_path.parent.mkdir()
    write_config(config_path)

    config = load_weaver_config(config_path)

    assert config.fabric.capacity.resource_group == "rg-test"
    assert config.fabric.workspace.source == (tmp_path / "etl" / "workspace" / "Test Workspace").resolve()
    assert config.fabric.lakehouse.repositories[0].source == (tmp_path / "app").resolve()
    assert config.fabric.ses.source == (tmp_path / "etl" / "SES").resolve()


def test_config_rejects_unknown_version(tmp_path: Path) -> None:
    config_path = tmp_path / "weaver.yaml"
    config_path.write_text("version: 99\n", encoding="utf-8")

    with pytest.raises(WeaverConfigError, match="unsupported config version"):
        load_weaver_config(config_path)


def test_capacity_az_args_are_config_driven() -> None:
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


def test_weaver_runtime_source_has_no_product_environment_defaults() -> None:
    disallowed = [
        "I Love Government",
        "T1",
        "T2",
        "Push to public store",
        "Build and Deploy ILG",
        "dwg-platform",
        "datawithoutguessing",
        "ilovegov",
    ]
    scanned = [
        ("src/weaver_runtime", "**/*.py"),
        ("docs", "**/*.md"),
        ("scripts", "**/*"),
        ("config", "**/*"),
    ]
    offenders = []
    for subdir, pattern in scanned:
        for path in sorted((ROOT / subdir).glob(pattern)):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            for needle in disallowed:
                if needle in text:
                    offenders.append(f"{path.relative_to(ROOT)}: {needle}")

    assert offenders == []
