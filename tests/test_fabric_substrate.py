from __future__ import annotations

from pathlib import Path

import pytest

from weaver_runtime.dbrep.fabric import transfer
from weaver_runtime.fabric import onelake
from weaver_runtime.fabric.settings import (
    DEFAULT_API_BASE_URL,
    resolve_settings,
)


def test_dbrep_snapshot_excludes_runtime_caches(tmp_path: Path) -> None:
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "cache.pyc").write_bytes(b"cache")
    (tmp_path / "keep.py").write_text("x", encoding="utf-8")
    (tmp_path / "old.pyo").write_bytes(b"cache")

    assert set(transfer.snapshot_tree(tmp_path)) == {"keep.py"}


def test_calculate_diff_uploads_changed_and_deletes_owned_extra() -> None:
    local = {
        "same.txt": {"sha256": "same", "size": 4},
        "changed.txt": {"sha256": "new", "size": 3},
    }
    remote = {
        "same.txt": {"sha256": "same", "size": 4},
        "changed.txt": {"sha256": "old", "size": 3},
        "deleted.txt": {"sha256": "deleted", "size": 7},
    }

    diff = transfer.calculate_diff(
        local, remote, {"same.txt", "changed.txt", "deleted.txt"}, delete=True
    )

    assert diff.upload_paths == ["changed.txt"]
    assert diff.delete_paths == ["deleted.txt"]
    assert diff.write_signatures is True


def test_fresh_dbrep_target_reads_as_empty(monkeypatch) -> None:
    from weaver_runtime.fabric.client import FabricClientError

    target = onelake.LakehouseTarget(
        workspace_id="w",
        lakehouse_id="l",
        storage_token="t",
        onelake_base_url="https://x",
    )

    def not_found(*args, **kwargs):
        raise FabricClientError("GET https://x returned HTTP 404: PathNotFound")

    monkeypatch.setattr(onelake, "read_file", not_found)
    monkeypatch.setattr(onelake, "list_files", not_found)
    assert transfer._read_remote_signatures(target, "T0") == {}
    assert transfer._list_remote_payload_paths(target, "T0") == set()


def test_validate_relative_path_rejects_traversal() -> None:
    for bad in ("../x", "/abs", "a/../b", "", "a\\b"):
        with pytest.raises(onelake.OneLakeError):
            onelake.validate_relative_path(bad)
    assert onelake.validate_relative_path("a/b/c.txt") == "a/b/c.txt"


def test_settings_resolution_order(monkeypatch) -> None:
    monkeypatch.delenv("FABRIC_API_BASE_URL", raising=False)
    assert resolve_settings().api_base_url == DEFAULT_API_BASE_URL
    monkeypatch.setenv("FABRIC_API_BASE_URL", "https://env.example")
    assert resolve_settings().api_base_url == "https://env.example"
    assert resolve_settings(api_base_url="https://cli.example").api_base_url == (
        "https://cli.example"
    )
