"""Folder staging contract and reconciliation (pure filesystem, no PySpark).

A Folder object produces files by writing them into a Weaver-issued *staging*
directory and returning the standard triplet ``(staging_folder, delete, messages)``:
everything under the staging folder is upserted, the explicit relative paths are
deleted, and the messages are supplementary log context. Weaver owns the
destination — it validates the triplet, reconciles the staged files against the
destination, counts file-level CRUD, and cleans the staging folder.

Reconciliation scans only the staged folder and the exact destination paths that
correspond to staged files or explicit deletes; it never inventories the whole
destination tree, so historical file collections are not rescanned and API
scratch pages are not counted unless the object deliberately stages them.
"""

from __future__ import annotations

import filecmp
import os
import shutil
import uuid
from pathlib import Path
from typing import Iterable

from ..errors import LoadError
from .logging import FILES, CrudCounts, require_triplet, validate_messages

# Weaver-owned files that object code may never stage, replace, or delete.
RESERVED_NAMES = frozenset({"_weaver.json"})
_GLOB_CHARS = set("*?[]")
_TMP_PREFIX = "._weaver_tmp_"


class StagingFolder:
    """A Weaver-issued staging directory with context-manager lifecycle.

    The directory is created up front and exposed as :attr:`path`. Used as a
    context manager it is removed on an *exceptional* exit and preserved on a
    normal exit, so object code may return the triplet either inside or after the
    ``with`` block with identical behaviour. Weaver consumes the folder once (via
    :func:`validate_folder_triplet`) and removes it after reconciliation.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self._consumed = False

    def __enter__(self) -> "StagingFolder":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc_type is not None:
            shutil.rmtree(self.path, ignore_errors=True)
        return False


def new_staging_folder(staging_root: Path) -> StagingFolder:
    """Create a unique empty staging directory beneath ``staging_root``."""

    path = Path(staging_root) / uuid.uuid4().hex
    path.mkdir(parents=True, exist_ok=False)
    return StagingFolder(path)


# --- Validation ------------------------------------------------------------


def validate_folder_triplet(
    result,
    *,
    issued: Iterable[StagingFolder],
    destination: Path | None = None,
) -> tuple[Path, tuple[str, ...], tuple[dict, ...]]:
    """Validate a ``Folder.read()`` triplet before any destination mutation.

    Returns ``(upsert_path, delete, messages)`` normalised for reconciliation.
    Enforces that the first item is the StagingFolder Weaver issued to this
    object (unconsumed, still on disk), that every delete entry is an exact
    relative file path (never absolute, traversing, a glob, a directory, or a
    reserved Weaver file), that nothing is both staged and deleted, and that all
    messages are structurally valid. Raises :class:`LoadError` on the first
    violation and marks the folder consumed on success.
    """

    staging_folder, delete_names, messages = require_triplet(result, "Folder")

    if not isinstance(staging_folder, StagingFolder):
        raise LoadError(
            "Folder.read() must return the StagingFolder from self.staging_folder() "
            "as the first value"
        )
    if not any(staging_folder is candidate for candidate in issued):
        raise LoadError(
            "Folder.read() returned a StagingFolder that Weaver did not issue to this object"
        )
    if staging_folder._consumed:
        raise LoadError("Folder.read() returned a StagingFolder that was already consumed")
    if not staging_folder.path.is_dir():
        raise LoadError(f"Folder staging directory does not exist: {staging_folder.path}")

    staged = set(staged_relative_files(staging_folder.path))
    for relative in staged:
        if Path(relative).name in RESERVED_NAMES:
            raise LoadError(f"reserved Weaver file cannot be staged: {relative}")

    deletes = _validate_delete_paths(delete_names, staged, destination)
    validated_messages = validate_messages(messages)

    staging_folder._consumed = True
    return staging_folder.path, deletes, validated_messages


def _validate_delete_paths(delete, staged: set[str], destination) -> tuple[str, ...]:
    if isinstance(delete, (str, bytes)):
        raise LoadError("Folder deletes must be a sequence of relative file names")
    try:
        entries = list(delete)
    except TypeError as exc:
        raise LoadError("Folder deletes must be a sequence of relative file names") from exc

    normalised: list[str] = []
    for raw in entries:
        if not isinstance(raw, str) or not raw.strip():
            raise LoadError("Folder delete entries must be non-empty path strings")
        if raw.endswith("/") or "\\" in raw:
            raise LoadError(f"Folder delete path must be an exact file, not a directory: {raw!r}")
        if any(char in _GLOB_CHARS for char in raw):
            raise LoadError(f"Folder delete path must be an exact file, not a glob: {raw!r}")
        path = Path(raw)
        if path.is_absolute() or raw.startswith("/"):
            raise LoadError(f"Folder delete path must be relative, not absolute: {raw!r}")
        if ".." in path.parts:
            raise LoadError(f"Folder delete path must not traverse with '..': {raw!r}")
        if path.name in RESERVED_NAMES:
            raise LoadError(f"reserved Weaver file cannot be deleted: {raw!r}")
        if path.name == "":
            raise LoadError(f"Folder delete path must name a file: {raw!r}")
        relative = path.as_posix()
        if relative in staged:
            raise LoadError(f"path cannot be both staged and deleted: {relative}")
        if destination is not None and (Path(destination) / path).is_dir():
            raise LoadError(f"Folder delete path is a directory, not a file: {raw!r}")
        normalised.append(relative)
    return tuple(normalised)


# --- Reconciliation --------------------------------------------------------


def apply_folder_result(upsert_path: Path, delete, destination: Path) -> CrudCounts:
    """Reconcile validated staged files into ``destination`` and count file CRUD.

    Applies staged creates and updates, then explicit deletes, then removes the
    staging folder. Only staged leaf files and explicit delete paths are touched;
    the destination tree is never fully rescanned. An identical staged file is
    counted as ``read`` but not created/updated.
    """

    destination = Path(destination)
    upsert_path = Path(upsert_path)
    try:
        staged = staged_relative_files(upsert_path)

        proposed: list[tuple[str, str]] = []
        for relative in staged:
            source = upsert_path / relative
            target = destination / relative
            if not target.exists():
                proposed.append((relative, "created"))
            elif not _files_identical(source, target):
                proposed.append((relative, "updated"))
            else:
                proposed.append((relative, "read"))

        created = updated = 0
        for relative, action in proposed:
            if action == "created":
                _safe_replace(upsert_path / relative, destination / relative)
                created += 1
            elif action == "updated":
                _safe_replace(upsert_path / relative, destination / relative)
                updated += 1

        deleted = 0
        for relative in delete:
            if Path(relative).name in RESERVED_NAMES:
                continue
            target = destination / relative
            if target.is_file():
                target.unlink()
                deleted += 1

        return CrudCounts(
            unit=FILES,
            read=len(staged),
            created=created,
            updated=updated,
            deleted=deleted,
        )
    finally:
        shutil.rmtree(upsert_path, ignore_errors=True)


def staged_relative_files(upsert_path: Path) -> list[str]:
    """Relative POSIX paths of every leaf file under a staging folder.

    Directories are not returned; only files are CRUD units.
    """

    upsert_path = Path(upsert_path)
    files: list[str] = []
    for dirpath, _dirnames, filenames in os.walk(upsert_path):
        for name in filenames:
            full = Path(dirpath) / name
            files.append(full.relative_to(upsert_path).as_posix())
    return sorted(files)


def _files_identical(source: Path, target: Path) -> bool:
    try:
        if source.stat().st_size != target.stat().st_size:
            return False
    except OSError:
        return False
    return filecmp.cmp(source, target, shallow=False)


def _safe_replace(source: Path, target: Path) -> None:
    """Copy ``source`` into place at ``target`` via a temporary sibling + rename."""

    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.parent / f"{_TMP_PREFIX}{uuid.uuid4().hex}"
    try:
        shutil.copyfile(source, tmp)
        os.replace(tmp, target)
    finally:
        if tmp.exists():
            tmp.unlink()
