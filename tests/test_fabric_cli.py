from __future__ import annotations

import json
from pathlib import Path

import pytest

from weaver_runtime.cli import main


def _write(path: Path, text: str = "x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _platform_config(tmp_path: Path) -> Path:
    for name in ("dwg-site-kit", "dwg", "ilovegov-dwg", "weaver"):
        _write(tmp_path / name / "keep.py")
        _write(tmp_path / name / "__pycache__" / "junk.pyc")
    config = tmp_path / "weaver.yaml"
    config.write_text(
        """
version: 1
fabric:
  lakehouse:
    workspace: WS
    name: LH
  platform:
    target_root: dwg-platform
    sources:
      - name: dwg-site-kit
        source: dwg-site-kit
        target: dwg-site-kit
      - name: dwg
        source: dwg
        target: dwg
      - name: ilovegov-dwg
        source: ilovegov-dwg
        target: ilovegov-dwg
      - name: weaver
        source: weaver
        target: weaver
""",
        encoding="utf-8",
    )
    return config


def test_onelake_sync_dry_run_respects_weaverignore(tmp_path: Path, capsys) -> None:
    source = tmp_path / "src"
    _write(source / "keep.py")
    _write(source / "__pycache__" / "x.pyc")
    (source / ".weaverignore").write_text("__pycache__/\n*.pyc\n", encoding="utf-8")
    config = tmp_path / "weaver.yaml"
    config.write_text("version: 1\nfabric:\n  lakehouse:\n    workspace: WS\n    name: LH\n", encoding="utf-8")

    code = main(
        [
            "fabric", "onelake", "sync",
            "--config", str(config),
            "--source", str(source),
            "--target-folder", "dwg-platform/weaver",
            "--dry-run",
        ]
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["operation"] == "fabric.onelake.sync"
    assert payload["target_folder"] == "Files/dwg-platform/weaver"
    assert set(payload["paths"]) == {".weaverignore", "keep.py"}


def test_platform_push_dry_run_planned_layout(tmp_path: Path, capsys) -> None:
    config = _platform_config(tmp_path)
    code = main(["fabric", "platform", "push", "--config", str(config), "--dry-run"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["operation"] == "fabric.platform.push"
    assert payload["planned_targets"] == [
        "Files/dwg-platform/dwg-site-kit",
        "Files/dwg-platform/dwg",
        "Files/dwg-platform/ilovegov-dwg",
        "Files/dwg-platform/weaver",
    ]
    # Excluded content is not counted as local.
    for result in payload["results"]:
        assert result["files"]["local"] == 1


def test_platform_push_partial_by_name(tmp_path: Path, capsys) -> None:
    config = _platform_config(tmp_path)
    code = main(
        ["fabric", "platform", "push", "--config", str(config), "--dry-run", "--name", "weaver"]
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["planned_targets"] == ["Files/dwg-platform/weaver"]


def test_platform_push_unknown_name_errors(tmp_path: Path, capsys) -> None:
    config = _platform_config(tmp_path)
    code = main(
        ["fabric", "platform", "push", "--config", str(config), "--dry-run", "--name", "nope"]
    )
    assert code == 1
    assert "unknown platform sources" in capsys.readouterr().err


def test_sql_execute_json_safe_coerces_non_native_values() -> None:
    import datetime
    import decimal

    from weaver_runtime.fabric_cli import _json_safe

    assert _json_safe(datetime.datetime(2026, 7, 10, 4, 32, 0)) == "2026-07-10 04:32:00"
    assert _json_safe(datetime.date(2026, 7, 10)) == "2026-07-10"
    assert _json_safe(decimal.Decimal("1.5")) == "1.5"
    assert _json_safe(b"\x00\x01") == "0001"
    assert _json_safe(None) is None
    assert _json_safe(3) == 3


def test_livy_submit_requires_code_or_file(tmp_path: Path, capsys) -> None:
    config = tmp_path / "weaver.yaml"
    config.write_text("version: 1\nfabric:\n  lakehouse:\n    workspace: WS\n    name: LH\n", encoding="utf-8")
    code = main(["fabric", "livy", "submit", "--config", str(config)])
    assert code == 1
    assert "--file or --code" in capsys.readouterr().err
