from __future__ import annotations

import json
from pathlib import Path

from weaver_runtime.cli import build_parser, main


def test_workspace_push_dry_run_uses_explicit_coordinates(
    tmp_path: Path, capsys
) -> None:
    source = tmp_path / "workspace"
    source.mkdir()
    (source / "Publish.ipynb").write_text("{}", encoding="utf-8")

    code = main(
        [
            "fabric",
            "workspace",
            "push",
            "--source",
            str(source),
            "--workspace-name",
            "Test Workspace",
            "--dry-run",
        ]
    )

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["workspace_name"] == "Test Workspace"
    assert payload["items"] == [
        {
            "name": "Publish",
            "source_path": str(source / "Publish.ipynb"),
            "action": "would_push",
        }
    ]


def test_retained_fabric_commands_parse_explicit_coordinates() -> None:
    parser = build_parser()
    workspace = parser.parse_args(
        [
            "fabric",
            "workspace",
            "push",
            "--source",
            "workspace",
            "--workspace-id",
            "workspace-id",
            "--dry-run",
        ]
    )
    notebook = parser.parse_args(
        [
            "fabric",
            "notebook",
            "run",
            "--workspace-name",
            "Workspace",
            "--name",
            "Notebook",
        ]
    )
    capacity = parser.parse_args(
        [
            "fabric",
            "capacity",
            "status",
            "--resource-group",
            "rg-test",
            "--capacity-name",
            "cap-test",
        ]
    )

    assert workspace.source == Path("workspace")
    assert workspace.workspace_id == "workspace-id"
    assert notebook.workspace_name == "Workspace"
    assert notebook.name == "Notebook"
    assert capacity.resource_group == "rg-test"
    assert capacity.capacity_name == "cap-test"


def test_notebook_run_forwards_explicit_parameters(monkeypatch) -> None:
    captured = {}

    def fake_run(script_name, argv):
        captured["script_name"] = script_name
        captured["argv"] = argv
        return 0

    monkeypatch.setattr("weaver_runtime.cli.run_legacy_main", fake_run)

    code = main(
        [
            "fabric",
            "notebook",
            "run",
            "--workspace-name",
            "Workspace",
            "--name",
            "Publish",
            "--parameter",
            "view_names=DWG.View",
            "--no-wait",
        ]
    )

    assert code == 0
    assert captured["script_name"] == "run_fabric_notebook_job"
    assert captured["argv"][:6] == [
        "--notebook",
        "Publish",
        "--workspace-name",
        "Workspace",
        "--parameter",
        "view_names=DWG.View",
    ]
    assert "--no-wait" in captured["argv"]
