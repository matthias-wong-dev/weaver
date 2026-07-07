from __future__ import annotations

import subprocess
import sys
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

from sync_git_repo import DEFAULT_WORKERS, calculate_diff, git_visible_files  # noqa: E402


def test_default_workers_is_32() -> None:
    assert DEFAULT_WORKERS == 32


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True)


def test_git_visible_files_include_tracked_and_non_ignored_untracked(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init")

    (repo / ".gitignore").write_text(".lakehouse/\n", encoding="utf-8")
    (repo / "tracked.txt").write_text("tracked\n", encoding="utf-8")
    (repo / "deleted.txt").write_text("deleted\n", encoding="utf-8")
    git(repo, "add", ".gitignore", "tracked.txt", "deleted.txt")
    (repo / "deleted.txt").unlink()

    (repo / "visible.txt").write_text("visible\n", encoding="utf-8")
    (repo / ".lakehouse").mkdir()
    (repo / ".lakehouse" / "ignored.txt").write_text("ignored\n", encoding="utf-8")

    assert git_visible_files(repo) == [".gitignore", "tracked.txt", "visible.txt"]


def test_calculate_diff_uploads_changed_and_missing_files_and_deletes_extra_files() -> None:
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

    diff = calculate_diff(
        local_signatures=local,
        remote_signatures=remote,
        remote_payload_paths={"same.txt", "changed.txt", "deleted.txt"},
    )

    assert diff.upload_paths == ["changed.txt", "missing_remote.txt"]
    assert diff.delete_paths == ["deleted.txt"]
    assert diff.write_signatures is True


def test_calculate_diff_noops_when_payload_and_signatures_match() -> None:
    local = {
        "a.txt": {"sha256": "a", "size": 1},
        "b.txt": {"sha256": "b", "size": 1},
    }

    diff = calculate_diff(
        local_signatures=local,
        remote_signatures=dict(local),
        remote_payload_paths={"a.txt", "b.txt"},
    )

    assert diff.upload_paths == []
    assert diff.delete_paths == []
    assert diff.write_signatures is False
