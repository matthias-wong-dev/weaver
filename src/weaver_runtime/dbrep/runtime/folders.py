"""Folder staging contract and reconciliation (pure filesystem, no PySpark).

A Folder object produces files by writing them into a Weaver-issued *staging*
directory and returning the standard pair ``(staging_folder, delete)``:
everything under the staging folder is upserted, the explicit relative paths are
deleted. Weaver owns the destination — it validates the result, reconciles the staged files against the
destination, counts file-level CRUD, and cleans the staging folder.

Incremental reconciliation scans the staged folder and exact explicit-delete
paths. Complete reconciliation inventories only destination leaf files that
match the Folder's declared File keys; non-matching files remain outside the
managed population.
"""

from __future__ import annotations

import filecmp
import fnmatch
import os
import shutil
import uuid
from pathlib import Path
from typing import Iterable

from ..errors import LoadError
from .logging import FILES, CrudCounts, require_load_pair

# Weaver-owned files that object code may never stage, replace, or delete.
RESERVED_NAMES = frozenset({"_weaver.json"})
_GLOB_CHARS = set("*?[]")
_TMP_PREFIX = "._weaver_tmp_"


class StagingFolder:
    """A Weaver-issued staging directory with context-manager lifecycle.

    The directory is created up front and exposed as :attr:`path`. Used as a
    context manager it is removed on an *exceptional* exit and preserved on a
    normal exit, so object code may return the pair either inside or after the
    ``with`` block with identical behaviour. Weaver consumes the folder once (via
    :func:`validate_folder_result`) and removes it after reconciliation.
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


def validate_folder_result(
    result,
    *,
    issued: Iterable[StagingFolder],
    file_keys: tuple[str, ...],
    is_incremental: bool,
    destination: Path | None = None,
) -> tuple[Path, tuple[str, ...]]:
    """Validate a ``Folder.read()`` result before any destination mutation.

    Returns ``(upsert_path, delete)`` normalised for reconciliation.
    Enforces that the first item is the StagingFolder Weaver issued to this
    object (unconsumed, still on disk), that staged and deleted files match a
    declared File key, that every delete entry is an exact relative file path
    (never absolute, traversing, a glob, a directory, or a reserved Weaver
    file), and that nothing is both staged and deleted. Raises
    :class:`LoadError` on the first violation and marks the folder consumed.
    """

    staging_folder, delete_names = require_load_pair(result, "Folder")

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

    managed = set(managed_relative_files(staging_folder.path, file_keys))
    if managed != staged:
        unmatched = sorted(staged - managed)
        raise LoadError(f"staged files do not match File key: {unmatched}")

    deletes = _validate_delete_paths(
        delete_names,
        staged,
        destination,
        file_keys=file_keys,
        is_incremental=is_incremental,
    )
    staging_folder._consumed = True
    return staging_folder.path, deletes


def _validate_delete_paths(
    delete,
    staged: set[str],
    destination,
    *,
    file_keys: tuple[str, ...],
    is_incremental: bool,
) -> tuple[str, ...]:
    if isinstance(delete, (str, bytes)):
        raise LoadError("Folder deletes must be a sequence of relative file names")
    try:
        entries = list(delete)
    except TypeError as exc:
        raise LoadError("Folder deletes must be a sequence of relative file names") from exc

    if not is_incremental and entries:
        raise LoadError("Non-incremental Folder cannot return explicit deletes")

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
        if not _matches_file_key(relative, file_keys):
            raise LoadError(f"Folder delete path does not match File key: {relative}")
        if relative in staged:
            raise LoadError(f"path cannot be both staged and deleted: {relative}")
        if destination is not None and (Path(destination) / path).is_dir():
            raise LoadError(f"Folder delete path is a directory, not a file: {raw!r}")
        normalised.append(relative)
    return tuple(normalised)


# --- Reconciliation --------------------------------------------------------


def apply_folder_result(
    upsert_path: Path,
    delete,
    destination: Path,
    *,
    file_keys: tuple[str, ...],
    is_incremental: bool,
) -> CrudCounts:
    """Reconcile validated staged files into ``destination`` and count file CRUD.

    Applies staged creates and updates, then explicit or automatic deletes, then
    removes the staging folder. Automatic deletion inventories only managed
    destination files. An identical staged file is counted as ``read`` but not
    created/updated.
    """

    destination = Path(destination)
    upsert_path = Path(upsert_path)
    try:
        staged = managed_relative_files(upsert_path, file_keys)

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

        delete_paths = set(delete)
        if not is_incremental:
            existing_managed = set(managed_relative_files(destination, file_keys))
            delete_paths.update(existing_managed - set(staged))

        deleted = 0
        for relative in sorted(delete_paths):
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


def managed_relative_files(root: Path, patterns: tuple[str, ...]) -> list[str]:
    """Unique managed leaf-file paths beneath ``root``, sorted as POSIX paths."""

    root = Path(root)
    matches: set[str] = set()
    for pattern in patterns:
        for path in root.glob(pattern):
            if path.is_file() and path.name not in RESERVED_NAMES:
                matches.add(path.relative_to(root).as_posix())
    return sorted(matches)


def _matches_file_key(relative: str, patterns: tuple[str, ...]) -> bool:
    path_parts = tuple(Path(relative).as_posix().split("/"))
    return any(_match_parts(path_parts, tuple(pattern.split("/"))) for pattern in patterns)


def _match_parts(path_parts: tuple[str, ...], pattern_parts: tuple[str, ...]) -> bool:
    if not pattern_parts:
        return not path_parts
    head, *tail = pattern_parts
    remaining = tuple(tail)
    if head == "**":
        return _match_parts(path_parts, remaining) or (
            bool(path_parts) and _match_parts(path_parts[1:], pattern_parts)
        )
    return bool(path_parts) and fnmatch.fnmatchcase(path_parts[0], head) and _match_parts(
        path_parts[1:], remaining
    )


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
