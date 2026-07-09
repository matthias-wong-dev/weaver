"""Static SQL dependency discovery.

References are collected from relation positions (``from``/``join``/``apply``)
and returned as tuples of identifier parts with bracket/quote delimiters
removed:

* ``Schema.Object``                    two-part, current database
* ``Database.Schema.Object``           three-part, explicit database
* ``Server.Database.Schema.Object``    four-part, external

Single-part names (CTEs, temp tables, aliases) are not relations we manage and
are ignored.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import sqlparse
from sqlparse.exceptions import SQLParseError
from sqlparse import tokens as T

_FROM_BOUNDARY_KEYWORDS = {
    "FOR",
    "GO",
    "GROUP",
    "HAVING",
    "OPTION",
    "ORDER",
    "UNION",
    "EXCEPT",
    "INTERSECT",
    "WHERE",
}

_STATEMENT_START_KEYWORDS = {
    "ALTER",
    "CREATE",
    "DELETE",
    "DROP",
    "INSERT",
    "MERGE",
    "SELECT",
    "SET",
    "TRUNCATE",
    "UPDATE",
    "USE",
}


@dataclass(frozen=True)
class _FlatToken:
    value: str
    normalized: str
    ttype: object
    start: int
    depth: int


def extract_sql_references(sql_text: str) -> tuple[tuple[str, ...], ...]:
    """Return ordered, de-duplicated 2/3/4-part relation references."""

    try:
        tokens = _flatten(sql_text)
    except SQLParseError:
        return _extract_sql_references_fallback(sql_text)
    references: list[tuple[str, ...]] = []
    seen: set[tuple[str, ...]] = set()

    for index, token in enumerate(tokens):
        if not _is_keyword(token):
            continue
        head = _keyword_head(token)
        words = set(token.normalized.split())
        if head == "FROM":
            for parts in _from_relations(sql_text, tokens, index):
                _add(references, seen, parts)
        elif head in {"APPLY", "USING"} or "JOIN" in words:
            nxt = _next_significant(tokens, index + 1)
            if nxt is not None:
                parts = _parse_name(sql_text, tokens[nxt].start)
                if parts is not None:
                    _add(references, seen, parts)

    return tuple(references)


def _extract_sql_references_fallback(sql_text: str) -> tuple[tuple[str, ...], ...]:
    """Lightweight relation scanner for very large SQL bodies."""

    references: list[tuple[str, ...]] = []
    seen: set[tuple[str, ...]] = set()
    relation_keyword = re.compile(
        r"\b(from|join|apply|using)\b", flags=re.IGNORECASE
    )
    for match in relation_keyword.finditer(sql_text):
        parts = _parse_name(sql_text, match.end())
        if parts is not None:
            _add(references, seen, parts)
    return tuple(references)


def _add(
    references: list[tuple[str, ...]],
    seen: set[tuple[str, ...]],
    parts: tuple[str, ...],
) -> None:
    if parts not in seen:
        seen.add(parts)
        references.append(parts)


def _from_relations(
    sql_text: str,
    tokens: list[_FlatToken],
    from_index: int,
) -> list[tuple[str, ...]]:
    depth = tokens[from_index].depth
    first = _next_significant(tokens, from_index + 1)
    if first is None:
        return []

    relations: list[tuple[str, ...]] = []
    parts = _parse_name(sql_text, tokens[first].start)
    if parts is not None:
        relations.append(parts)

    for index in range(first + 1, len(tokens)):
        token = tokens[index]
        if token.depth < depth:
            break
        if token.depth != depth:
            continue
        if _is_from_boundary(token):
            break
        if token.value != ",":
            continue
        nxt = _next_significant(tokens, index + 1)
        if nxt is None or tokens[nxt].depth != depth:
            continue
        parts = _parse_name(sql_text, tokens[nxt].start)
        if parts is not None:
            relations.append(parts)

    return relations


def _is_from_boundary(token: _FlatToken) -> bool:
    if token.value == ";":
        return True
    if not _is_keyword(token):
        return False
    head = _keyword_head(token)
    if head in _FROM_BOUNDARY_KEYWORDS:
        return True
    return head in _STATEMENT_START_KEYWORDS and head != "SELECT"


def _parse_name(sql_text: str, start: int) -> tuple[str, ...] | None:
    position = _skip_space(sql_text, start)
    parts: list[str] = []

    while position < len(sql_text):
        parsed = _parse_identifier_part(sql_text, position)
        if parsed is None:
            break
        part, position = parsed
        parts.append(part)
        position = _skip_space(sql_text, position)
        if position >= len(sql_text) or sql_text[position] != ".":
            break
        position = _skip_space(sql_text, position + 1)
        if len(parts) >= 4:
            break

    if len(parts) < 2 or len(parts) > 4:
        return None
    if any(part.startswith(("#", "@")) or not part for part in parts):
        return None
    return tuple(parts)


def _parse_identifier_part(sql_text: str, start: int) -> tuple[str, int] | None:
    if start >= len(sql_text):
        return None
    character = sql_text[start]
    if character == "[":
        return _parse_delimited(sql_text, start, "]")
    if character == '"':
        return _parse_delimited(sql_text, start, '"')
    match = re.match(r"[A-Za-z_@#][A-Za-z0-9_@$#]*", sql_text[start:])
    if not match:
        return None
    return match.group(0), start + match.end()


def _parse_delimited(sql_text: str, start: int, closer: str) -> tuple[str, int] | None:
    position = start + 1
    characters: list[str] = []
    while position < len(sql_text):
        character = sql_text[position]
        if character == closer:
            if position + 1 < len(sql_text) and sql_text[position + 1] == closer:
                characters.append(closer)
                position += 2
                continue
            return "".join(characters), position + 1
        characters.append(character)
        position += 1
    return None


def _skip_space(sql_text: str, start: int) -> int:
    position = start
    while position < len(sql_text) and sql_text[position] in " \t\r\n":
        position += 1
    return position


def _flatten(sql_text: str) -> list[_FlatToken]:
    flat: list[_FlatToken] = []
    offset = 0
    depth = 0
    for statement in sqlparse.parse(sql_text):
        for token in statement.flatten():
            value = token.value
            token_depth = depth
            if value == ")":
                depth = max(0, depth - 1)
                token_depth = depth
            flat.append(
                _FlatToken(
                    value=value,
                    normalized=token.normalized.upper(),
                    ttype=token.ttype,
                    start=offset,
                    depth=token_depth,
                )
            )
            offset += len(value)
            if value == "(":
                depth += 1
    return flat


def _next_significant(tokens: list[_FlatToken], index: int) -> int | None:
    for candidate in range(index, len(tokens)):
        if not _is_trivia(tokens[candidate]):
            return candidate
    return None


def _is_trivia(token: _FlatToken) -> bool:
    return token.ttype in T.Whitespace or token.ttype in T.Comment


def _is_keyword(token: _FlatToken) -> bool:
    return token.ttype in T.Keyword


def _keyword_head(token: _FlatToken) -> str:
    parts = token.normalized.split(maxsplit=1)
    return parts[0] if parts else ""
