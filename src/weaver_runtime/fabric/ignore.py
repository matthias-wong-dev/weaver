"""gitignore-style path matching with no Git binary required.

Supports ``.weaverignore`` and ``.gitignore`` files read from the root of a
synced folder. Implements the common subset of gitignore semantics: comments,
blank lines, negation (``!``), directory-only patterns (trailing ``/``),
anchored patterns (leading or embedded ``/``), and ``*``/``?``/``**`` globs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

#: Weaver honours only ``.weaverignore``. ``.gitignore`` is intentionally not
#: read: platform sync must not depend on Git state or a Git working tree.
IGNORE_FILENAMES = (".weaverignore",)

#: Baseline exclusions always applied to platform/code sync, so standard build
#: and tooling junk never reaches OneLake even when a source repo lacks its own
#: ``.weaverignore``. Product/operation paths are never listed here.
DEFAULT_PLATFORM_IGNORE_LINES = [
    ".git/",
    ".DS_Store",
    "__pycache__/",
    "*.pyc",
    "*.pyo",
    ".venv/",
    "venv/",
    ".env",
    ".pytest_cache/",
    ".mypy_cache/",
    ".ruff_cache/",
    ".ipynb_checkpoints/",
    "node_modules/",
    # Anchored to the source root: top-level build artifacts only, never a
    # legitimately-named source package like ``.../dbrep/build/``.
    "/dist/",
    "/build/",
    ".schema/",
    ".published/",
    ".workspaces/",
    ".lakehouse/",
]


@dataclass(frozen=True)
class _Rule:
    regex: re.Pattern[str]
    negation: bool
    dir_only: bool


def _translate(pattern: str) -> str:
    """Translate a gitignore glob body into a regex matching a full path."""

    out: list[str] = []
    i = 0
    length = len(pattern)
    while i < length:
        char = pattern[i]
        if char == "*":
            if pattern[i : i + 2] == "**":
                # Collapse '**' (optionally with a following '/') into "any depth".
                i += 2
                if pattern[i : i + 1] == "/":
                    i += 1
                    out.append("(?:.*/)?")
                else:
                    out.append(".*")
                continue
            out.append("[^/]*")
        elif char == "?":
            out.append("[^/]")
        elif char == "/":
            out.append("/")
        else:
            out.append(re.escape(char))
        i += 1
    return "".join(out)


def _compile(line: str) -> _Rule | None:
    negation = line.startswith("!")
    if negation:
        line = line[1:]
    dir_only = line.endswith("/")
    if dir_only:
        line = line[:-1]
    if not line:
        return None

    # Anchored if a slash appears at the start or middle (not just trailing).
    anchored = line.startswith("/") or "/" in line
    body = _translate(line.lstrip("/"))
    prefix = "" if anchored else "(?:.*/)?"
    return _Rule(
        regex=re.compile(f"{prefix}{body}"),
        negation=negation,
        dir_only=dir_only,
    )


class IgnoreSpec:
    """A compiled set of ignore rules evaluated against relative posix paths."""

    def __init__(self, rules: list[_Rule]) -> None:
        self._rules = rules

    @property
    def empty(self) -> bool:
        return not self._rules

    def _ancestors(self, relative_path: str) -> list[str]:
        parts = relative_path.split("/")
        return ["/".join(parts[: index + 1]) for index in range(len(parts) - 1)]

    def match(self, relative_path: str, *, is_dir: bool = False) -> bool:
        """Return ``True`` if a file/dir at ``relative_path`` is ignored."""

        ancestors = self._ancestors(relative_path)
        ignored = False
        for rule in self._rules:
            if rule.dir_only:
                candidates = [*ancestors, relative_path] if is_dir else ancestors
            else:
                candidates = [relative_path, *ancestors]
            if any(rule.regex.fullmatch(candidate) for candidate in candidates):
                ignored = not rule.negation
        return ignored


def parse_ignore_lines(lines: list[str]) -> IgnoreSpec:
    """Compile ignore-file lines into a spec."""

    rules: list[_Rule] = []
    for raw in lines:
        line = raw.rstrip("\n").rstrip("\r")
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        # A leading backslash escapes '#'/'!'; strip it after the check.
        if line.startswith("\\"):
            line = line[1:]
        rule = _compile(line.rstrip())
        if rule is not None:
            rules.append(rule)
    return IgnoreSpec(rules)


def load_ignore_spec(root: Path) -> IgnoreSpec:
    """Load ``.weaverignore`` rules from ``root`` (no Git files, no Git binary)."""

    lines: list[str] = []
    for name in IGNORE_FILENAMES:
        path = root / name
        if path.is_file():
            lines.extend(path.read_text(encoding="utf-8").splitlines())
    return parse_ignore_lines(lines)


def default_platform_ignore_spec() -> IgnoreSpec:
    """Return the baseline ignore spec applied to all platform/code sync."""

    return parse_ignore_lines(list(DEFAULT_PLATFORM_IGNORE_LINES))
