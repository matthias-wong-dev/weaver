"""Static Python dependency discovery.

The canonical dependency expression is ``self.repo["Schema.Object"]`` (intra) or
``self.repo["Database.Schema.Object"]`` (cross). References are found by static
AST inspection; object modules are never imported and aliases/wildcard imports
are intentionally not consulted.
"""

from __future__ import annotations

import ast

from ..errors import DependencyError


def extract_python_references(source: str) -> tuple[tuple[str, ...], ...]:
    """Return ordered, de-duplicated multi-part ``self.repo[...]`` references."""

    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise DependencyError(f"python object file is not parseable: {exc}") from exc

    references: list[tuple[str, ...]] = []
    seen: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Subscript) or not _is_self_repo(node.value):
            continue
        key = _constant_string(node.slice)
        if key is None:
            # A non-literal key cannot be resolved statically; ignore it.
            continue
        parts = tuple(part.strip() for part in key.split("."))
        if any(not part for part in parts):
            raise DependencyError(f"invalid self.repo reference: {key!r}")
        if len(parts) < 2:
            raise DependencyError(
                f"self.repo reference must be at least Schema.Object: {key!r}"
            )
        if key not in seen:
            seen.add(key)
            references.append(parts)
    return tuple(references)


def _is_self_repo(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "repo"
        and isinstance(node.value, ast.Name)
        and node.value.id == "self"
    )


def _constant_string(slice_node: ast.AST) -> str | None:
    node = slice_node
    if isinstance(node, ast.Index):  # Python < 3.9 compatibility
        node = node.value
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None
