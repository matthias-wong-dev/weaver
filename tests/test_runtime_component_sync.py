from __future__ import annotations

import json
from pathlib import Path

import pytest

from weaver_runtime.dbrep.fabric import onelake as dbrep_onelake
from weaver_runtime.dbrep.lakehouse.artifacts import RuntimeComponent
from weaver_runtime.fabric import onelake, sync
from weaver_runtime.fabric.ignore import default_platform_ignore_spec


def _resolved() -> dict:
    return {
        "workspace_id": "workspace",
        "lakehouse_id": "Lake",
        "workspace_name": "Workspace",
        "lakehouse_name": "Lake",
        "storage_token": "token",
        "onelake_base_url": "https://onelake.example",
    }


def _target() -> onelake.LakehouseTarget:
    return onelake.LakehouseTarget(
        workspace_id="workspace",
        lakehouse_id="Lake",
        storage_token="token",
        onelake_base_url="https://onelake.example",
    )


def test_runtime_sync_uses_independent_components_and_individual_metadata(
    tmp_path: Path, monkeypatch
) -> None:
    files = tmp_path / "Files"
    runtime = files / "_weaver" / "runtime"
    orchestrator = runtime / "_orchestrator"
    database = runtime / "objects" / "T0"
    materialisation = files / "T0"
    for folder in (orchestrator, database, materialisation):
        folder.mkdir(parents=True)
        (folder / "keep.py").write_text("x", encoding="utf-8")
    for name in dbrep_onelake.RUNTIME_METADATA_NAMES:
        (runtime / name).write_text("{}\n", encoding="utf-8")

    components = (
        RuntimeComponent("builtin", "weaver", orchestrator, "_weaver/runtime/_orchestrator"),
        RuntimeComponent("database", "T0", database, "_weaver/runtime/objects/T0"),
    )
    sync_calls = []
    uploads = []

    def fake_sync(target, source, remote, **kwargs):
        sync_calls.append((Path(source), remote, kwargs))
        return {"files": {"uploaded": 1}}

    monkeypatch.setattr(sync, "sync_folder", fake_sync)
    monkeypatch.setattr(
        dbrep_onelake,
        "upload_file",
        lambda resolved, path, content: uploads.append((path, content)),
    )

    uploaded = dbrep_onelake.sync_runtime_folder(
        files, _resolved(), runtime_components=components
    )

    assert [call[1] for call in sync_calls] == [
        "_weaver/runtime/_orchestrator",
        "_weaver/runtime/objects/T0",
        "T0",
    ]
    assert all(call[2]["delete"] for call in sync_calls[:2])
    assert sync_calls[2][2]["delete"] is False
    assert all(
        call[2]["extra_ignore"].match("pkg/__pycache__", is_dir=True)
        for call in sync_calls[:2]
    )
    assert all(
        call[2]["extra_ignore"].match("pkg/cache.pyo") for call in sync_calls[:2]
    )
    assert [path for path, _content in uploads] == [
        f"_weaver/runtime/{name}" for name in dbrep_onelake.RUNTIME_METADATA_NAMES
    ]
    assert uploaded == 10


def test_runtime_cache_files_are_excluded_and_old_remote_caches_deleted(
    tmp_path: Path, monkeypatch
) -> None:
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "old.pyc").write_bytes(b"cache")
    (tmp_path / "old.pyo").write_bytes(b"cache")
    (tmp_path / "keep.py").write_text("keep", encoding="utf-8")
    deleted = []

    monkeypatch.setattr(sync, "_read_remote_signatures", lambda *args: {})
    monkeypatch.setattr(
        sync,
        "_list_remote_payload_paths",
        lambda *args: {"__pycache__/old.pyc", "old.pyo"},
    )
    monkeypatch.setattr(onelake, "ensure_directory", lambda *args: None)
    monkeypatch.setattr(onelake, "upload_file", lambda *args, **kwargs: None)
    monkeypatch.setattr(onelake, "delete_file", lambda target, path: deleted.append(path))
    monkeypatch.setattr(sync, "remove_empty_directories", lambda *args: [])

    result = sync.sync_folder(
        _target(),
        tmp_path,
        "_weaver/runtime/objects/T0",
        respect_ignore=False,
        extra_ignore=default_platform_ignore_spec(),
        delete=True,
    )

    assert result["files"]["ignored"] == 2
    assert result["deleted_paths"] == ["__pycache__/old.pyc", "old.pyo"]
    assert sorted(deleted) == [
        "_weaver/runtime/objects/T0/__pycache__/old.pyc",
        "_weaver/runtime/objects/T0/old.pyo",
    ]


def test_empty_directory_cleanup_is_scoped_and_preserves_zero_byte_file(monkeypatch) -> None:
    prefix = "Lake.Lakehouse/Files/_weaver/runtime/objects/T0"
    paths = [
        {"name": f"{prefix}/empty", "isDirectory": "true"},
        {"name": f"{prefix}/empty/nested", "isDirectory": "true"},
        {"name": f"{prefix}/kept", "isDirectory": True},
        {
            "name": f"{prefix}/kept/zero.bin",
            "isDirectory": "false",
            "contentLength": "0",
        },
    ]
    deleted = []
    monkeypatch.setattr(onelake, "list_paths", lambda *args: paths)
    monkeypatch.setattr(
        onelake, "delete_directory", lambda target, path: deleted.append(path) or True
    )

    removed = sync.remove_empty_directories(
        _target(), "_weaver/runtime/objects/T0"
    )

    assert removed == ["empty/nested", "empty"]
    assert deleted == [
        "Files/_weaver/runtime/objects/T0/empty/nested",
        "Files/_weaver/runtime/objects/T0/empty",
    ]


def test_empty_directory_cleanup_rejects_paths_outside_component(monkeypatch) -> None:
    monkeypatch.setattr(
        onelake,
        "list_paths",
        lambda *args: [{"name": "Lake.Lakehouse/Files/other/escape", "isDirectory": True}],
    )
    monkeypatch.setattr(
        onelake, "delete_directory", lambda *args: pytest.fail("must not delete")
    )
    with pytest.raises(onelake.OneLakeError, match="outside target"):
        sync.remove_empty_directories(_target(), "_weaver/runtime/objects/T0")


def test_read_runtime_metadata_reads_only_named_json_documents(monkeypatch) -> None:
    requested = []

    def fake_read(target, path):
        requested.append(path)
        return json.dumps({"path": path}).encode()

    monkeypatch.setattr(onelake, "read_file", fake_read)
    documents = dbrep_onelake.read_runtime_metadata(_resolved())

    assert tuple(documents) == dbrep_onelake.RUNTIME_METADATA_NAMES
    assert requested == [
        f"_weaver/runtime/{name}" for name in dbrep_onelake.RUNTIME_METADATA_NAMES
    ]
