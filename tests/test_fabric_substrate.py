from __future__ import annotations

from pathlib import Path

import pytest

from weaver_runtime.fabric import onelake, sync
from weaver_runtime.fabric.ignore import parse_ignore_lines
from weaver_runtime.fabric.settings import (
    DEFAULT_API_BASE_URL,
    FabricSettings,
    resolve_settings,
)


# --- ignore semantics ------------------------------------------------------


def test_ignore_directory_file_and_negation() -> None:
    spec = parse_ignore_lines(
        ["build/", "*.log", "/root-only.txt", "!keep.log", "a/b/c.txt"]
    )
    assert spec.match("build/x/y.txt")
    assert spec.match("foo.log")
    assert spec.match("nested/foo.log")
    assert not spec.match("keep.log")
    assert spec.match("root-only.txt")
    assert not spec.match("nested/root-only.txt")
    assert spec.match("a/b/c.txt")
    assert not spec.match("x/a/b/c.txt")


def test_ignore_comments_and_blank_lines() -> None:
    spec = parse_ignore_lines(["", "# a comment", "  ", "*.pyc"])
    assert spec.match("x.pyc")
    assert not spec.match("x.py")


# --- weaverignore acceptance (criterion E) ---------------------------------


def test_weaverignore_acceptance_fixture(tmp_path: Path) -> None:
    sample = tmp_path / "sample"
    (sample / ".schema").mkdir(parents=True)
    (sample / ".published").mkdir()
    (sample / "__pycache__").mkdir()
    (sample / "keep.py").write_text("x", encoding="utf-8")
    (sample / "keep.html").write_text("x", encoding="utf-8")
    (sample / ".weaverignore").write_text(
        ".schema/\n.published/\n__pycache__/\n*.pyc\n", encoding="utf-8"
    )
    (sample / ".schema" / "junk.json").write_text("x", encoding="utf-8")
    (sample / ".published" / "junk.html").write_text("x", encoding="utf-8")
    (sample / "__pycache__" / "junk.pyc").write_text("x", encoding="utf-8")

    snapshots = sync.snapshot_folder(sample, respect_ignore=True)

    included = set(snapshots)
    assert "keep.py" in included
    assert "keep.html" in included
    assert ".schema/junk.json" not in included
    assert ".published/junk.html" not in included
    assert "__pycache__/junk.pyc" not in included


def test_snapshot_ignores_reserved_signatures_file(tmp_path: Path) -> None:
    (tmp_path / "signatures.json").write_text("{}", encoding="utf-8")
    (tmp_path / "keep.txt").write_text("x", encoding="utf-8")
    assert set(sync.snapshot_folder(tmp_path)) == {"keep.txt"}


def test_gitignore_is_not_honoured(tmp_path: Path) -> None:
    # Only .weaverignore drives sync exclusions; .gitignore is deliberately ignored.
    (tmp_path / "keep.log").write_text("x", encoding="utf-8")
    (tmp_path / ".gitignore").write_text("*.log\n", encoding="utf-8")
    included = set(sync.snapshot_folder(tmp_path, respect_ignore=True))
    assert "keep.log" in included
    assert ".gitignore" in included

    (tmp_path / ".weaverignore").write_text("*.log\n", encoding="utf-8")
    included = set(sync.snapshot_folder(tmp_path, respect_ignore=True))
    assert "keep.log" not in included


def test_baseline_build_dir_is_root_anchored(tmp_path: Path) -> None:
    from weaver_runtime.fabric.ignore import default_platform_ignore_spec

    (tmp_path / "build").mkdir()
    (tmp_path / "build" / "artifact.txt").write_text("x", encoding="utf-8")
    (tmp_path / "pkg" / "build").mkdir(parents=True)
    (tmp_path / "pkg" / "build" / "mod.py").write_text("x", encoding="utf-8")

    included = set(
        sync.snapshot_folder(
            tmp_path, respect_ignore=False, extra_ignore=default_platform_ignore_spec()
        )
    )
    # Top-level build/ artifacts excluded; a nested source package named build kept.
    assert "build/artifact.txt" not in included
    assert "pkg/build/mod.py" in included


def test_snapshot_reports_ignored_paths(tmp_path: Path) -> None:
    (tmp_path / "keep.py").write_text("x", encoding="utf-8")
    (tmp_path / "junk").mkdir()
    (tmp_path / "junk" / "a.txt").write_text("x", encoding="utf-8")
    (tmp_path / ".weaverignore").write_text("junk/\n", encoding="utf-8")
    ignored: list[str] = []
    sync.snapshot_folder(tmp_path, respect_ignore=True, ignored_out=ignored)
    assert "junk/" in ignored


# --- signature diff --------------------------------------------------------


def test_calculate_diff_uploads_changed_and_missing_and_deletes_extra() -> None:
    local = {
        "same.txt": {"sha256": "same", "size": 4},
        "changed.txt": {"sha256": "new", "size": 3},
        "missing_remote.txt": {"sha256": "new-file", "size": 8},
    }
    remote = {
        "same.txt": {"sha256": "same", "size": 4},
        "changed.txt": {"sha256": "old", "size": 3},
        "deleted.txt": {"sha256": "deleted", "size": 7},
    }
    diff = sync.calculate_diff(local, remote, {"same.txt", "changed.txt", "deleted.txt"})
    assert diff.upload_paths == ["changed.txt", "missing_remote.txt"]
    assert diff.delete_paths == ["deleted.txt"]
    assert diff.write_signatures is True


def test_calculate_diff_noop_when_matching() -> None:
    local = {"a.txt": {"sha256": "a", "size": 1}, "b.txt": {"sha256": "b", "size": 1}}
    diff = sync.calculate_diff(local, dict(local), {"a.txt", "b.txt"})
    assert diff.upload_paths == []
    assert diff.delete_paths == []
    assert diff.write_signatures is False


def test_calculate_diff_delete_disabled_keeps_extra() -> None:
    diff = sync.calculate_diff(
        {"a.txt": {"sha256": "a", "size": 1}},
        {},
        {"a.txt", "extra.txt"},
        delete=False,
    )
    assert diff.delete_paths == []


# --- onelake urls / path safety --------------------------------------------


def test_validate_relative_path_rejects_traversal() -> None:
    for bad in ("../x", "/abs", "a/../b", "", "a\\b"):
        with pytest.raises(onelake.OneLakeError):
            onelake.validate_relative_path(bad)
    assert onelake.validate_relative_path("a/b/c.txt") == "a/b/c.txt"


def test_file_url_uuid_vs_named_lakehouse() -> None:
    named = onelake.file_url("https://onelake.example", "ws", "MyLake", "a/b.txt")
    assert "MyLake.Lakehouse" in named
    uuid_id = "11111111-1111-1111-1111-111111111111"
    by_uuid = onelake.file_url("https://onelake.example", "ws", uuid_id, "a/b.txt")
    assert f"/{uuid_id}/Files/a/b.txt" in by_uuid


# --- settings resolution ---------------------------------------------------


def test_settings_defaults() -> None:
    settings = resolve_settings()
    assert settings.api_base_url == DEFAULT_API_BASE_URL
    assert settings.default_degrees_of_parallelism == 32


def test_settings_resolution_order(monkeypatch) -> None:
    config = FabricSettings(api_base_url="https://config.example")
    # CLI override wins over config.
    assert resolve_settings(config, api_base_url="https://cli.example").api_base_url == (
        "https://cli.example"
    )
    # Config wins over default when no CLI/env.
    monkeypatch.delenv("FABRIC_API_BASE_URL", raising=False)
    assert resolve_settings(config).api_base_url == "https://config.example"
    # Env var overrides config when no CLI override.
    monkeypatch.setenv("FABRIC_API_BASE_URL", "https://env.example")
    assert resolve_settings(config).api_base_url == "https://env.example"
