"""Folder staging, pair validation, and reconciliation (no Spark)."""

from __future__ import annotations

from pathlib import Path

import pytest

from weaver_runtime.dbrep.errors import LoadError
from weaver_runtime.dbrep.runtime.context import LoadContext, Repo
from weaver_runtime.dbrep.runtime.folders import (
    StagingFolder,
    apply_folder_result,
    new_staging_folder,
    staged_relative_files,
    validate_folder_result,
)


def _context(staging_root: Path) -> LoadContext:
    return LoadContext(
        runtime_root=Path("/runtime"),
        lakehouse_root=Path("/lake"),
        object_id="T0.Raw.Drop",
        kind="Folder",
        materialisation="Files/T0/Raw/Drop",
        repo=Repo(),
        staging_root=staging_root,
    )


def _stage(staging_root: Path, files: dict[str, str]) -> StagingFolder:
    staging = new_staging_folder(staging_root)
    for relative, content in files.items():
        path = staging.path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return staging


def _validate(staging: StagingFolder, *, delete=(), destination=None):
    return validate_folder_result((staging, delete), issued=[staging], destination=destination)


# --- Staging-folder lifecycle ----------------------------------------------


def test_normal_context_exit_preserves_staging(tmp_path: Path) -> None:
    with new_staging_folder(tmp_path / "staging") as staging:
        (staging.path / "a.csv").write_text("x", encoding="utf-8")
    assert staging.path.is_dir()  # preserved for Weaver to consume


def test_exceptional_context_exit_cleans_staging(tmp_path: Path) -> None:
    captured: dict[str, Path] = {}
    with pytest.raises(RuntimeError):
        with new_staging_folder(tmp_path / "staging") as staging:
            captured["path"] = staging.path
            raise RuntimeError("boom")
    assert not captured["path"].exists()


def test_return_inside_and_outside_with_are_equivalent(tmp_path: Path) -> None:
    # Return-after-with (staging created, block exits normally, then validated).
    outside = _stage(tmp_path / "s1", {"a.csv": "one"})
    up1, del1 = _validate(outside)
    counts_outside = apply_folder_result(up1, del1, tmp_path / "d1")

    # Return-inside-with: identical because normal exit also preserves.
    with new_staging_folder(tmp_path / "s2") as inside:
        (inside.path / "a.csv").write_text("one", encoding="utf-8")
        pair = (inside, ())
    up2, del2 = validate_folder_result(pair, issued=[inside], destination=tmp_path / "d2")
    counts_inside = apply_folder_result(up2, del2, tmp_path / "d2")

    assert counts_outside == counts_inside
    assert (counts_inside.read, counts_inside.created) == (1, 1)


def test_context_cleanup_staging_removes_issued_dirs(tmp_path: Path) -> None:
    context = _context(tmp_path / "staging")
    first = context.staging_folder()
    second = context.staging_folder()
    assert first.path.is_dir() and second.path.is_dir()
    context.cleanup_staging()
    assert not first.path.exists() and not second.path.exists()


# --- Shared pair shape -----------------------------------------------------


def test_valid_pair_accepted(tmp_path: Path) -> None:
    staging = _stage(tmp_path / "staging", {"a.csv": "one"})
    upsert_path, deletes = _validate(staging)
    assert upsert_path == staging.path
    assert deletes == ()


def test_non_tuple_result_rejected(tmp_path: Path) -> None:
    staging = _stage(tmp_path / "staging", {"a.csv": "x"})
    with pytest.raises(LoadError, match="exactly two values"):
        validate_folder_result(staging, issued=[staging])


def test_too_few_items_rejected(tmp_path: Path) -> None:
    staging = _stage(tmp_path / "staging", {"a.csv": "x"})
    with pytest.raises(LoadError, match="exactly two values"):
        validate_folder_result((staging,), issued=[staging])


def test_too_many_items_rejected(tmp_path: Path) -> None:
    staging = _stage(tmp_path / "staging", {"a.csv": "x"})
    with pytest.raises(LoadError, match="exactly two values"):
        validate_folder_result((staging, (), ()), issued=[staging])


def test_first_item_must_be_staging_folder(tmp_path: Path) -> None:
    staging = _stage(tmp_path / "staging", {"a.csv": "x"})
    with pytest.raises(LoadError, match="StagingFolder"):
        validate_folder_result((staging.path, ()), issued=[staging])


def test_unissued_staging_folder_rejected(tmp_path: Path) -> None:
    issued = _stage(tmp_path / "issued", {"a.csv": "x"})
    other = _stage(tmp_path / "other", {"a.csv": "x"})
    with pytest.raises(LoadError, match="did not issue"):
        validate_folder_result((other, ()), issued=[issued])


def test_already_consumed_staging_folder_rejected(tmp_path: Path) -> None:
    staging = _stage(tmp_path / "staging", {"a.csv": "x"})
    _validate(staging)  # first consumption marks it consumed
    with pytest.raises(LoadError, match="already consumed"):
        _validate(staging)


# --- Delete-path validation ------------------------------------------------


def test_absolute_delete_rejected(tmp_path: Path) -> None:
    staging = _stage(tmp_path / "staging", {"a.csv": "x"})
    with pytest.raises(LoadError):
        _validate(staging, delete=["/etc/passwd"])


def test_traversal_delete_rejected(tmp_path: Path) -> None:
    staging = _stage(tmp_path / "staging", {"a.csv": "x"})
    with pytest.raises(LoadError):
        _validate(staging, delete=["../secret.csv"])


def test_glob_delete_rejected(tmp_path: Path) -> None:
    staging = _stage(tmp_path / "staging", {"a.csv": "x"})
    with pytest.raises(LoadError):
        _validate(staging, delete=["*.csv"])


def test_staged_and_deleted_same_path_rejected(tmp_path: Path) -> None:
    staging = _stage(tmp_path / "staging", {"a.csv": "x"})
    with pytest.raises(LoadError):
        _validate(staging, delete=["a.csv"])


def test_reserved_weaver_file_cannot_be_staged(tmp_path: Path) -> None:
    staging = _stage(tmp_path / "staging", {"_weaver.json": "{}"})
    with pytest.raises(LoadError):
        _validate(staging)


def test_reserved_weaver_file_cannot_be_deleted(tmp_path: Path) -> None:
    staging = _stage(tmp_path / "staging", {"a.csv": "x"})
    with pytest.raises(LoadError):
        _validate(staging, delete=["_weaver.json"])


def test_directory_delete_rejected(tmp_path: Path) -> None:
    staging = _stage(tmp_path / "staging", {"a.csv": "x"})
    destination = tmp_path / "dest"
    (destination / "archive").mkdir(parents=True)
    with pytest.raises(LoadError):
        _validate(staging, delete=["archive"], destination=destination)


def test_trailing_slash_delete_rejected(tmp_path: Path) -> None:
    staging = _stage(tmp_path / "staging", {"a.csv": "x"})
    with pytest.raises(LoadError):
        _validate(staging, delete=["archive/"])


# --- Reconciliation --------------------------------------------------------


def test_new_staged_file_is_created(tmp_path: Path) -> None:
    staging = _stage(tmp_path / "staging", {"a.csv": "one"})
    upsert_path, deletes = _validate(staging)
    counts = apply_folder_result(upsert_path, deletes, tmp_path / "dest")
    assert (counts.read, counts.created, counts.updated) == (1, 1, 0)
    assert (tmp_path / "dest" / "a.csv").read_text() == "one"


def test_different_staged_file_is_updated(tmp_path: Path) -> None:
    destination = tmp_path / "dest"
    destination.mkdir()
    (destination / "a.csv").write_text("old", encoding="utf-8")

    staging = _stage(tmp_path / "staging", {"a.csv": "new"})
    upsert_path, deletes = _validate(staging)
    counts = apply_folder_result(upsert_path, deletes, destination)
    assert (counts.read, counts.created, counts.updated) == (1, 0, 1)
    assert (destination / "a.csv").read_text() == "new"


def test_identical_staged_file_is_read_only(tmp_path: Path) -> None:
    destination = tmp_path / "dest"
    destination.mkdir()
    (destination / "a.csv").write_text("same", encoding="utf-8")

    staging = _stage(tmp_path / "staging", {"a.csv": "same"})
    upsert_path, deletes = _validate(staging)
    counts = apply_folder_result(upsert_path, deletes, destination)
    assert (counts.read, counts.created, counts.updated) == (1, 0, 0)


def test_unwanted_file_is_deleted(tmp_path: Path) -> None:
    destination = tmp_path / "dest"
    destination.mkdir()
    (destination / "unwanted.json").write_text("gone", encoding="utf-8")

    staging = _stage(tmp_path / "staging", {"a.csv": "x"})
    upsert_path, deletes = _validate(staging, delete=["unwanted.json"])
    counts = apply_folder_result(upsert_path, deletes, destination)
    assert counts.deleted == 1
    assert not (destination / "unwanted.json").exists()


def test_absent_delete_counts_zero(tmp_path: Path) -> None:
    staging = _stage(tmp_path / "staging", {"a.csv": "x"})
    upsert_path, deletes = _validate(staging, delete=["missing.csv"])
    counts = apply_folder_result(upsert_path, deletes, tmp_path / "dest")
    assert counts.deleted == 0


def test_nested_leaf_files_count_individually(tmp_path: Path) -> None:
    staging = _stage(
        tmp_path / "staging", {"sub/a.csv": "1", "sub/deep/b.csv": "2", "c.csv": "3"}
    )
    assert staged_relative_files(staging.path) == ["c.csv", "sub/a.csv", "sub/deep/b.csv"]
    upsert_path, deletes = _validate(staging)
    counts = apply_folder_result(upsert_path, deletes, tmp_path / "dest")
    assert (counts.read, counts.created) == (3, 3)


def test_empty_directories_do_not_count(tmp_path: Path) -> None:
    staging = new_staging_folder(tmp_path / "staging")
    (staging.path / "empty").mkdir()
    (staging.path / "a.csv").write_text("x", encoding="utf-8")
    upsert_path, deletes = _validate(staging)
    counts = apply_folder_result(upsert_path, deletes, tmp_path / "dest")
    assert counts.read == 1


def test_staging_cleaned_after_success(tmp_path: Path) -> None:
    staging = _stage(tmp_path / "staging", {"a.csv": "x"})
    upsert_path, deletes = _validate(staging)
    apply_folder_result(upsert_path, deletes, tmp_path / "dest")
    assert not staging.path.exists()


def test_staging_cleaned_after_reconciliation_failure(tmp_path: Path) -> None:
    staging = _stage(tmp_path / "staging", {"a.csv": "x"})
    upsert_path, deletes = _validate(staging)
    destination = tmp_path / "dest_is_a_file"
    destination.write_text("blocking", encoding="utf-8")
    with pytest.raises(Exception):
        apply_folder_result(upsert_path, deletes, destination)
    assert not staging.path.exists()


def test_destination_unchanged_when_validation_fails(tmp_path: Path) -> None:
    destination = tmp_path / "dest"
    destination.mkdir()
    (destination / "a.csv").write_text("orig", encoding="utf-8")

    staging = _stage(tmp_path / "staging", {"a.csv": "new"})
    with pytest.raises(LoadError):
        _validate(staging, delete=["/absolute.csv"], destination=destination)
    assert (destination / "a.csv").read_text() == "orig"
