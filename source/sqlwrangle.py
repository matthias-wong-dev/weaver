"""Utilities for transforming T-SQL text."""

from __future__ import annotations

from dataclasses import dataclass

import sqlparse
from sqlparse import tokens as T


_BOUNDARY_KEYWORDS = {
    "GO",
    "GROUP",
    "HAVING",
    "ORDER",
    "UNION",
    "EXCEPT",
    "INTERSECT",
    "OPTION",
    "FOR",
}

_STATEMENT_START_KEYWORDS = {
    "ALTER",
    "CREATE",
    "DECLARE",
    "DELETE",
    "DROP",
    "EXEC",
    "EXECUTE",
    "IF",
    "INSERT",
    "MERGE",
    "PRINT",
    "RAISERROR",
    "RETURN",
    "SELECT",
    "SET",
    "THROW",
    "TRUNCATE",
    "UPDATE",
    "USE",
    "WAITFOR",
    "WHILE",
}


@dataclass(frozen=True)
class _FlatToken:
    value: str
    normalized: str
    ttype: object
    start: int
    end: int
    depth: int


@dataclass(frozen=True)
class _Replacement:
    start: int
    end: int
    text: str


def insert_where_one_eq_zero(sql_text: str) -> str:
    """Insert ``WHERE 1=0`` into every SELECT in a T-SQL string.

    If a SELECT already has a WHERE clause, its existing condition is wrapped in
    parentheses and combined with ``AND 1=0``.
    """

    replacements = _collect_replacements(sql_text)
    if not replacements:
        return sql_text

    result = sql_text
    for replacement in sorted(replacements, key=lambda item: item.start, reverse=True):
        result = (
            result[: replacement.start] + replacement.text + result[replacement.end :]
        )
    return result


def insert_ctas(sql_text: str, table_name: str) -> str:
    """Prefix the last standalone SELECT query with Fabric CTAS syntax."""

    query_span = _find_last_standalone_query_span(sql_text)
    if query_span is None:
        return sql_text

    start, _end = query_span
    return f"{sql_text[:start]}CREATE TABLE {table_name} AS\n{sql_text[start:]}"


def _collect_replacements(sql_text: str) -> list[_Replacement]:
    tokens = _flatten_with_offsets(sql_text)
    replacements: list[_Replacement] = []
    covered_ranges: list[tuple[int, int]] = []

    for index, token in enumerate(tokens):
        if not _is_select(token):
            continue

        if _is_covered(token.start, covered_ranges):
            continue

        replacement = _replacement_for_select(sql_text, tokens, index)
        if replacement is None:
            continue

        replacements = [
            item
            for item in replacements
            if not (replacement.start <= item.start and item.end <= replacement.end)
        ]
        if replacement.start != replacement.end:
            covered_ranges.append((replacement.start, replacement.end))
        replacements.append(replacement)

    return replacements


def _find_last_standalone_query_span(sql_text: str) -> tuple[int, int] | None:
    tokens = _flatten_with_offsets(sql_text)
    spans: list[tuple[int, int, int]] = []

    for index, token in enumerate(tokens):
        if token.depth != 0:
            continue

        if _is_select(token) and _is_standalone_select_start(tokens, index):
            spans.append((token.start, _find_query_end(tokens, index), token.start))
            continue

        if _keyword_head(token) == "WITH" and _is_statement_boundary_before(
            tokens, index
        ):
            select_index = _find_cte_body_select(tokens, index)
            if select_index is not None:
                spans.append((token.start, _find_query_end(tokens, select_index), token.start))

    if not spans:
        return None

    start, end, _position = max(spans, key=lambda item: item[2])
    return start, end


def _find_cte_body_select(tokens: list[_FlatToken], with_index: int) -> int | None:
    for index in range(with_index + 1, len(tokens)):
        token = tokens[index]
        if token.depth != 0:
            continue
        if _is_select(token):
            return index
        if token.value == ";" or _keyword_head(token) == "GO":
            return None
        if _is_statement_starter(token) and _keyword_head(token) not in {"WITH", "SELECT"}:
            return None
    return None


def _find_query_end(tokens: list[_FlatToken], select_index: int) -> int:
    select_token = tokens[select_index]

    for index in range(select_index + 1, len(tokens)):
        token = tokens[index]
        if token.depth < select_token.depth:
            return token.start
        if token.depth != select_token.depth:
            continue
        if token.value == ";" or _keyword_head(token) == "GO":
            return token.end if token.value == ";" else _end_before_trivia(tokens, select_index, index)
        if _is_statement_starter(token) and not _is_set_operator_select(tokens, index):
            return _end_before_trivia(tokens, select_index, index)

    return _end_before_trivia(tokens, select_index, len(tokens))


def _is_standalone_select_start(tokens: list[_FlatToken], index: int) -> bool:
    if _is_set_operator_select(tokens, index):
        return False
    if _has_top_level_with_since_boundary(tokens, index):
        return False
    if _is_statement_boundary_before(tokens, index):
        return True
    if not _starts_new_line(tokens, index):
        return False

    starter = _last_statement_starter_since_boundary(tokens, index)
    if starter is None:
        return True
    if _keyword_head(tokens[starter]) == "INSERT":
        return _has_top_level_select_between(tokens, starter + 1, index)
    return True


def _is_set_operator_select(tokens: list[_FlatToken], index: int) -> bool:
    previous = _previous_significant_index(tokens, index)
    if previous is None:
        return False

    previous_keyword = _keyword_head(tokens[previous])
    if previous_keyword in {"UNION", "EXCEPT", "INTERSECT"}:
        return True
    if previous_keyword == "ALL":
        before_all = _previous_significant_index(tokens, previous)
        return before_all is not None and _keyword_head(tokens[before_all]) in {
            "UNION",
            "EXCEPT",
            "INTERSECT",
        }
    return False


def _is_statement_boundary_before(tokens: list[_FlatToken], index: int) -> bool:
    previous = _previous_significant_index(tokens, index)
    if previous is None:
        return True

    previous_token = tokens[previous]
    return previous_token.value == ";" or _keyword_head(previous_token) == "GO"


def _starts_new_line(tokens: list[_FlatToken], index: int) -> bool:
    for previous in range(index - 1, -1, -1):
        token = tokens[previous]
        if token.depth != tokens[index].depth:
            continue
        if "\n" in token.value:
            return True
        if not _is_trivia(token):
            return False
    return True


def _last_statement_starter_since_boundary(
    tokens: list[_FlatToken], index: int
) -> int | None:
    for previous in range(index - 1, -1, -1):
        token = tokens[previous]
        if token.depth != tokens[index].depth or _is_trivia(token):
            continue
        if token.value == ";" or _keyword_head(token) == "GO":
            return None
        if _is_statement_starter(token):
            return previous
    return None


def _has_top_level_select_between(
    tokens: list[_FlatToken], start: int, end: int
) -> bool:
    return any(token.depth == 0 and _is_select(token) for token in tokens[start:end])


def _has_top_level_with_since_boundary(tokens: list[_FlatToken], index: int) -> bool:
    for previous in range(index - 1, -1, -1):
        token = tokens[previous]
        if token.depth != tokens[index].depth or _is_trivia(token):
            continue
        if token.value == ";" or _keyword_head(token) == "GO":
            return False
        if _keyword_head(token) == "WITH":
            return True
    return False


def _replacement_for_select(
    sql_text: str, tokens: list[_FlatToken], select_index: int
) -> _Replacement | None:
    select_token = tokens[select_index]
    scope_end_index = _find_scope_end(tokens, select_index)
    where_index = _find_where(tokens, select_index + 1, scope_end_index, select_token.depth)

    if where_index is None:
        insert_at = _find_insert_position(
            tokens, select_index + 1, scope_end_index, select_token.depth
        )
        return _Replacement(insert_at, insert_at, " WHERE 1=0")

    condition_start = tokens[where_index].end
    condition_end = _find_condition_end(
        tokens, where_index + 1, scope_end_index, select_token.depth
    )
    condition = sql_text[condition_start:condition_end].strip()
    transformed_condition = insert_where_one_eq_zero(condition) if condition else condition
    return _Replacement(
        condition_start,
        condition_end,
        f" ({transformed_condition}) AND 1=0",
    )


def _flatten_with_offsets(sql_text: str) -> list[_FlatToken]:
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
                    end=offset + len(value),
                    depth=token_depth,
                )
            )
            offset += len(value)

            if value == "(":
                depth += 1

    return flat


def _find_scope_end(tokens: list[_FlatToken], select_index: int) -> int:
    select_token = tokens[select_index]

    for index in range(select_index + 1, len(tokens)):
        token = tokens[index]
        if token.depth < select_token.depth:
            return index
        if token.depth == select_token.depth and _is_scope_terminator(token):
            return index

    return len(tokens)


def _find_where(
    tokens: list[_FlatToken], start: int, end: int, depth: int
) -> int | None:
    for index in range(start, end):
        token = tokens[index]
        if token.depth == depth and token.normalized == "WHERE":
            return index
    return None


def _find_insert_position(
    tokens: list[_FlatToken], start: int, end: int, depth: int
) -> int:
    for index in range(start, end):
        token = tokens[index]
        if token.depth == depth and _is_boundary(token):
            return _end_before_trivia(tokens, start, index)

    if end < len(tokens):
        return _end_before_trivia(tokens, start, end)
    if tokens:
        return _end_before_trivia(tokens, start, len(tokens))
    return 0


def _find_condition_end(
    tokens: list[_FlatToken], start: int, end: int, depth: int
) -> int:
    for index in range(start, end):
        token = tokens[index]
        if token.depth == depth and _is_boundary(token):
            return _end_before_trivia(tokens, start, index)

    if end < len(tokens):
        return _end_before_trivia(tokens, start, end)
    if tokens:
        return _end_before_trivia(tokens, start, len(tokens))
    return 0


def _end_before_trivia(tokens: list[_FlatToken], start: int, end: int) -> int:
    index = end - 1
    while index >= start and tokens[index].ttype in T.Whitespace:
        index -= 1
    if index >= start:
        return tokens[index].end
    return tokens[start].start if start < len(tokens) else 0


def _is_select(token: _FlatToken) -> bool:
    return token.ttype is T.DML and token.normalized == "SELECT"


def _is_boundary(token: _FlatToken) -> bool:
    return token.value == ";" or _keyword_head(token) in _BOUNDARY_KEYWORDS


def _is_scope_terminator(token: _FlatToken) -> bool:
    if token.value == ";":
        return True

    keyword = _keyword_head(token)
    return keyword in {"GO", "UNION", "EXCEPT", "INTERSECT"} or (
        _is_statement_starter(token) and keyword != "SELECT"
    ) or _is_select(token)


def _is_statement_starter(token: _FlatToken) -> bool:
    return _keyword_head(token) in _STATEMENT_START_KEYWORDS


def _keyword_head(token: _FlatToken) -> str:
    parts = token.normalized.split(maxsplit=1)
    return parts[0] if parts else ""


def _previous_significant_index(
    tokens: list[_FlatToken], index: int, depth: int = 0
) -> int | None:
    for previous in range(index - 1, -1, -1):
        token = tokens[previous]
        if token.depth != depth or _is_trivia(token):
            continue
        return previous
    return None


def _is_trivia(token: _FlatToken) -> bool:
    return token.ttype in T.Whitespace or token.ttype in T.Comment


def _is_covered(position: int, ranges: list[tuple[int, int]]) -> bool:
    return any(start <= position < end for start, end in ranges)
