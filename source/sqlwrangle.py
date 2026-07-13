"""Utilities for transforming T-SQL text."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from string import Template

import sqlparse
from sqlparse import tokens as T


SQL_TEMPLATE_DIR = Path(__file__).resolve().parent / "sql_templates"
SqlDependency = tuple[str, ...]


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

_FROM_DEPENDENCY_BOUNDARY_KEYWORDS = {
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


@dataclass(frozen=True)
class _QuerySpan:
    start: int
    end: int
    select_index: int


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

    query_span = _find_last_standalone_query(sql_text)
    if query_span is None:
        return sql_text

    return (
        f"{sql_text[:query_span.start]}create table {table_name} as\n"
        f"{sql_text[query_span.start:]}"
    )


def insert_ranked_ctas(
    sql_text: str,
    table_name: str,
    *,
    partition_columns: str,
    rank_column: str,
) -> str:
    """Materialise the last result query with one persisted group rank.

    The source query is captured once in an internal CTE, so unions and existing
    CTEs retain their result semantics while ranking is evaluated only once.
    """

    query_span = _find_last_standalone_query(sql_text)
    if query_span is None:
        return sql_text

    tokens = _flatten_with_offsets(sql_text)
    select_start = tokens[query_span.select_index].start
    statement_prefix = sql_text[query_span.start:select_start]
    result_query = sql_text[select_start:query_span.end].strip().rstrip(";").rstrip()
    source_cte = "[__weaver_rank_source]"

    if statement_prefix.lstrip().upper().startswith("WITH"):
        cte_prefix = statement_prefix.rstrip() + ",\n"
    else:
        cte_prefix = "with "
        result_query = sql_text[query_span.start:query_span.end].strip().rstrip(";").rstrip()

    ranked_sql = (
        f"create table {table_name} as\n"
        f"{cte_prefix}{source_cte} as (\n"
        f"{result_query}\n"
        ")\n"
        "select\n"
        "    s.*\n"
        "  , row_number() over (\n"
        f"        partition by {partition_columns}\n"
        "        order by (select 0)\n"
        f"    ) as {rank_column}\n"
        f"from {source_cte} as s"
    )
    return f"{sql_text[:query_span.start]}{ranked_sql}{sql_text[query_span.end:]}"


def insert_select_into(sql_text: str, table_name: str) -> str:
    """Insert ``INTO <table_name>`` into the last standalone SELECT query."""

    query_span = _find_last_standalone_query(sql_text)
    if query_span is None:
        return sql_text

    tokens = _flatten_with_offsets(sql_text)
    insert_at = _find_select_into_insert_position(tokens, query_span)
    insert_text = _select_into_text(sql_text, insert_at, table_name)
    return f"{sql_text[:insert_at]}{insert_text}{sql_text[insert_at:]}"


def get_sql_template(template_name: str) -> str:
    """Fetch a SQL template from ``source/sql_templates``."""

    template_path = _sql_template_path(template_name)
    return template_path.read_text(encoding="utf-8")


def render_sql_template(template_name: str, **values: object) -> str:
    """Fetch and populate a SQL template with ``string.Template`` values."""

    template = Template(get_sql_template(template_name))
    return template.substitute({key: str(value) for key, value in values.items()})


def find_sql_dependencies(sql_text: str) -> frozenset[SqlDependency]:
    """Find two-part SES and three-part external object references in SQL.

    Dependencies are collected from relation positions such as ``from`` and
    ``join``. Each dependency is returned as a tuple of identifier parts with
    bracket or quote delimiters removed.
    """

    tokens = _flatten_with_offsets(sql_text)
    dependencies: set[SqlDependency] = set()

    for index, token in enumerate(tokens):
        if not _is_dependency_context(token):
            continue

        if _keyword_head(token) == "FROM":
            _add_from_dependencies(sql_text, tokens, index, dependencies)
            continue

        _add_next_dependency(sql_text, tokens, index, dependencies)

    return frozenset(dependencies)


def extract_sql_dependencies(sql_text: str) -> frozenset[SqlDependency]:
    """Alias for ``find_sql_dependencies``."""

    return find_sql_dependencies(sql_text)


def format_sql_dependency(dependency: SqlDependency) -> str:
    """Format a dependency tuple as a bracketed multipart SQL name."""

    return ".".join(_quote_dependency_part(part) for part in dependency)


def _is_dependency_context(token: _FlatToken) -> bool:
    keyword = _keyword_head(token)
    words = set(token.normalized.split())
    return (
        keyword in {"FROM", "APPLY", "USING", "EXEC", "EXECUTE"}
        or "JOIN" in words
    )


def _add_from_dependencies(
    sql_text: str,
    tokens: list[_FlatToken],
    from_index: int,
    dependencies: set[SqlDependency],
) -> None:
    from_depth = tokens[from_index].depth
    first_source = _next_significant_index(tokens, from_index + 1)
    if first_source is None:
        return

    _add_dependency_at_token(sql_text, tokens[first_source], dependencies)

    for index in range(first_source + 1, len(tokens)):
        token = tokens[index]
        if token.depth < from_depth:
            return
        if token.depth != from_depth:
            continue
        if _is_from_dependency_boundary(token):
            return
        if token.value != ",":
            continue

        next_source = _next_significant_index(tokens, index + 1)
        if next_source is None:
            return
        if tokens[next_source].depth != from_depth:
            continue
        _add_dependency_at_token(sql_text, tokens[next_source], dependencies)


def _add_next_dependency(
    sql_text: str,
    tokens: list[_FlatToken],
    context_index: int,
    dependencies: set[SqlDependency],
) -> None:
    next_index = _next_significant_index(tokens, context_index + 1)
    if next_index is None:
        return
    _add_dependency_at_token(sql_text, tokens[next_index], dependencies)


def _add_dependency_at_token(
    sql_text: str,
    token: _FlatToken,
    dependencies: set[SqlDependency],
) -> None:
    parsed = _parse_dependency_at(sql_text, token.start)
    if parsed is not None:
        dependencies.add(parsed)


def _parse_dependency_at(sql_text: str, start: int) -> SqlDependency | None:
    parsed = _parse_multipart_name_at(sql_text, start)
    if parsed is None:
        return None

    parts, _ = parsed
    if len(parts) not in {2, 3}:
        return None
    if any(part.startswith(("#", "@")) for part in parts):
        return None
    return parts


def _parse_multipart_name_at(
    sql_text: str,
    start: int,
) -> tuple[SqlDependency, int] | None:
    position = _skip_identifier_space(sql_text, start)
    parts: list[str] = []

    while position < len(sql_text):
        parsed_part = _parse_identifier_part(sql_text, position)
        if parsed_part is None:
            break

        part, position = parsed_part
        parts.append(part)
        position = _skip_identifier_space(sql_text, position)

        if position >= len(sql_text) or sql_text[position] != ".":
            break
        position = _skip_identifier_space(sql_text, position + 1)

        if len(parts) >= 4:
            break

    if not parts:
        return None
    if len(parts) > 3:
        return None
    return tuple(parts), position


def _parse_identifier_part(sql_text: str, start: int) -> tuple[str, int] | None:
    if start >= len(sql_text):
        return None

    character = sql_text[start]
    if character == "[":
        return _parse_bracketed_identifier_part(sql_text, start)
    if character == '"':
        return _parse_quoted_identifier_part(sql_text, start)
    return _parse_bare_identifier_part(sql_text, start)


def _parse_bracketed_identifier_part(
    sql_text: str,
    start: int,
) -> tuple[str, int] | None:
    position = start + 1
    characters: list[str] = []

    while position < len(sql_text):
        character = sql_text[position]
        if character == "]":
            if position + 1 < len(sql_text) and sql_text[position + 1] == "]":
                characters.append("]")
                position += 2
                continue
            return "".join(characters), position + 1
        characters.append(character)
        position += 1

    return None


def _parse_quoted_identifier_part(
    sql_text: str,
    start: int,
) -> tuple[str, int] | None:
    position = start + 1
    characters: list[str] = []

    while position < len(sql_text):
        character = sql_text[position]
        if character == '"':
            if position + 1 < len(sql_text) and sql_text[position + 1] == '"':
                characters.append('"')
                position += 2
                continue
            return "".join(characters), position + 1
        characters.append(character)
        position += 1

    return None


def _parse_bare_identifier_part(sql_text: str, start: int) -> tuple[str, int] | None:
    match = re.match(r"[A-Za-z_@#][A-Za-z0-9_@$#]*", sql_text[start:])
    if not match:
        return None
    return match.group(0), start + match.end()


def _skip_identifier_space(sql_text: str, start: int) -> int:
    position = start
    while position < len(sql_text) and sql_text[position] in " \t\r\n":
        position += 1
    return position


def _is_from_dependency_boundary(token: _FlatToken) -> bool:
    if token.value == ";":
        return True

    keyword = _keyword_head(token)
    return keyword in _FROM_DEPENDENCY_BOUNDARY_KEYWORDS or (
        _is_statement_starter(token) and keyword not in {"SELECT"}
    )


def _quote_dependency_part(part: str) -> str:
    return f"[{part.replace(']', ']]')}]"


def _sql_template_path(template_name: str) -> Path:
    normalised_name = template_name if template_name.endswith(".sql") else f"{template_name}.sql"
    candidate = (SQL_TEMPLATE_DIR / normalised_name).resolve()
    template_root = SQL_TEMPLATE_DIR.resolve()
    if template_root not in candidate.parents:
        raise ValueError("template_name must stay within the SQL template directory")
    if not candidate.is_file():
        raise FileNotFoundError(f"SQL template not found: {template_name}")
    return candidate


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


def _find_last_standalone_query(sql_text: str) -> _QuerySpan | None:
    tokens = _flatten_with_offsets(sql_text)
    spans: list[_QuerySpan] = []

    for index, token in enumerate(tokens):
        if token.depth != 0:
            continue

        if _is_select(token) and _is_standalone_select_start(tokens, index):
            spans.append(_QuerySpan(token.start, _find_query_end(tokens, index), index))
            continue

        if _keyword_head(token) == "WITH" and _is_statement_boundary_before(
            tokens, index
        ):
            select_index = _find_cte_body_select(tokens, index)
            if select_index is not None:
                spans.append(
                    _QuerySpan(
                        token.start, _find_query_end(tokens, select_index), select_index
                    )
                )

    if not spans:
        return None

    return max(spans, key=lambda item: item.start)


def _find_select_into_insert_position(
    tokens: list[_FlatToken], query_span: _QuerySpan
) -> int:
    select_token = tokens[query_span.select_index]

    for index in range(query_span.select_index + 1, len(tokens)):
        token = tokens[index]
        if token.start >= query_span.end:
            break
        if token.depth != select_token.depth:
            continue
        if _keyword_head(token) == "FROM":
            return _end_before_trivia(tokens, query_span.select_index + 1, index)
        if _is_select_into_boundary(token):
            return _end_before_trivia(tokens, query_span.select_index + 1, index)

    end_index = _token_index_at_or_after(tokens, query_span.end)
    return _end_before_trivia(tokens, query_span.select_index + 1, end_index)


def _select_into_text(sql_text: str, insert_at: int, table_name: str) -> str:
    if insert_at > 0 and sql_text[insert_at - 1] == "\n":
        return f"into {table_name}\n"
    next_non_space = insert_at
    while next_non_space < len(sql_text) and sql_text[next_non_space] in " \t\r\n":
        if sql_text[next_non_space] == "\n":
            return f"\ninto {table_name}"
        next_non_space += 1
    return f" into {table_name}"


def _is_select_into_boundary(token: _FlatToken) -> bool:
    keyword = _keyword_head(token)
    return token.value == ";" or keyword in {
        "WHERE",
        "GROUP",
        "HAVING",
        "ORDER",
        "UNION",
        "EXCEPT",
        "INTERSECT",
        "OPTION",
        "FOR",
        "GO",
    }


def _token_index_at_or_after(tokens: list[_FlatToken], position: int) -> int:
    for index, token in enumerate(tokens):
        if token.start >= position:
            return index
    return len(tokens)


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
        return _Replacement(insert_at, insert_at, " where 1=0")

    condition_start = tokens[where_index].end
    condition_end = _find_condition_end(
        tokens, where_index + 1, scope_end_index, select_token.depth
    )
    condition = sql_text[condition_start:condition_end].strip()
    transformed_condition = insert_where_one_eq_zero(condition) if condition else condition
    return _Replacement(
        condition_start,
        condition_end,
        f" ({transformed_condition}) and 1=0",
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


def _next_significant_index(tokens: list[_FlatToken], index: int) -> int | None:
    for next_index in range(index, len(tokens)):
        if _is_trivia(tokens[next_index]):
            continue
        return next_index
    return None


def _is_trivia(token: _FlatToken) -> bool:
    return token.ttype in T.Whitespace or token.ttype in T.Comment


def _is_covered(position: int, ranges: list[tuple[int, int]]) -> bool:
    return any(start <= position < end for start, end in ranges)
