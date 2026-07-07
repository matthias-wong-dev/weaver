#!/usr/bin/env python3
"""Create SES SQL files as Fabric/Azure SQL views."""

from __future__ import annotations

import re
import sys
import os
import argparse
from pathlib import Path

import pyodbc


SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from sqlserver import connect


DEFAULT_SES_DIR = Path(os.environ.get("SES_DIR", "SES"))
TABLE_ID_RE = re.compile(r"View ID:\s*([A-Za-z_][\w]*)\.([A-Za-z_][\w]*)")
BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
LINE_COMMENT_RE = re.compile(r"--.*?$", re.MULTILINE)
BRACKET_IDENTIFIER_RE = re.compile(r"\[[^\]]*(?:\]\][^\]]*)*\]")
TRAILING_OPTION_RE = re.compile(r"\s+OPTION\s*\([^)]*\)\s*$", re.IGNORECASE | re.DOTALL)
DEPENDENCY_ERROR_RE = re.compile(
    r"(invalid object name|could not use view or function|depends on missing|"
    r"cannot find the object|not found|invalid column name)",
    re.IGNORECASE,
)
MAX_CREATE_ATTEMPTS = 5


def quote_name(name: str) -> str:
    return "[" + name.replace("]", "]]") + "]"


def read_view_definition(path: Path) -> tuple[str, str, str]:
    text = path.read_text(encoding="utf-8")
    match = TABLE_ID_RE.search(text)
    if not match:
        raise ValueError(f"{path}: missing View ID: metadata")

    schema, view = match.groups()
    sql = BLOCK_COMMENT_RE.sub("", text).strip()
    sql = sql.rstrip(";").strip()
    sql = TRAILING_OPTION_RE.sub("", sql).strip()
    if not sql:
        raise ValueError(f"{path}: SQL body is empty")

    body_for_checks = LINE_COMMENT_RE.sub("", BLOCK_COMMENT_RE.sub("", sql))
    body_for_checks = BRACKET_IDENTIFIER_RE.sub("[]", body_for_checks)
    if not re.match(r"^\s*(SELECT|WITH)\b", body_for_checks, re.IGNORECASE):
        raise ValueError(f"{path}: SQL body must start with SELECT or WITH")

    return schema, view, sql


def sql_files(ses_dir: Path) -> list[Path]:
    return sorted(ses_dir.glob("*.sql"))


def display_path(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def create_schema(cursor, schema: str) -> None:
    escaped_schema = schema.replace("'", "''")
    cursor.execute(
        f"IF SCHEMA_ID(N'{escaped_schema}') IS NULL EXEC(N'CREATE SCHEMA {quote_name(schema)}')"
    )


def create_view(cursor, schema: str, view: str, sql: str) -> None:
    statement = f"CREATE OR ALTER VIEW {quote_name(schema)}.{quote_name(view)} AS\n{sql}"
    cursor.execute(statement)
    cursor.commit()


def is_dependency_error(exc: Exception) -> bool:
    text = " ".join(str(part) for part in getattr(exc, "args", (str(exc),)))
    return bool(DEPENDENCY_ERROR_RE.search(text))


def create_views_with_retries(
    cursor,
    definitions: list[tuple[Path, str, str, str]],
    display_root: Path,
) -> list[str]:
    pending = definitions[:]
    created = []
    deferred: dict[str, str] = {}

    for attempt in range(1, MAX_CREATE_ATTEMPTS + 1):
        if not pending:
            break

        print(
            f"create attempt {attempt}: {len(pending)} view(s) pending",
            flush=True,
        )
        next_pending = []
        created_this_attempt = 0

        for path, schema, view, sql in pending:
            view_name = f"{schema}.{view}"
            try:
                create_view(cursor, schema, view, sql)
            except pyodbc.Error as exc:
                if is_dependency_error(exc):
                    deferred[view_name] = str(exc)
                    next_pending.append((path, schema, view, sql))
                    print(f"deferred {view_name}: {exc}", flush=True)
                    continue
                raise

            created.append(view_name)
            created_this_attempt += 1
            deferred.pop(view_name, None)
            print(f"created {view_name} from {display_path(path, display_root)}", flush=True)

        pending = next_pending
        if not pending:
            break
        if created_this_attempt == 0:
            print("No deferred views succeeded on this attempt.", file=sys.stderr)
            for view_name, reason in deferred.items():
                print(f"unresolved {view_name}: {reason}", file=sys.stderr)
            raise RuntimeError(f"{len(next_pending)} view(s) remain unresolved")

    if pending:
        for path, schema, view, _ in pending:
            view_name = f"{schema}.{view}"
            print(
                f"unresolved {view_name} from {display_path(path, display_root)}: {deferred.get(view_name)}",
                file=sys.stderr,
            )
        raise RuntimeError(f"{len(pending)} view(s) remain unresolved")

    return created


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ses-dir", type=Path, default=DEFAULT_SES_DIR)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    ses_dir = args.ses_dir.resolve()
    files = sql_files(ses_dir)
    if not files:
        print(f"No SQL files found under {ses_dir}", file=sys.stderr)
        return 1

    definitions = [(path, *read_view_definition(path)) for path in files]
    schemas = sorted({schema for _, schema, _, _ in definitions})

    with connect() as conn:
        cursor = conn.cursor()
        for schema in schemas:
            create_schema(cursor, schema)
        conn.commit()

        created = create_views_with_retries(cursor, definitions, ses_dir)
        

    print(f"Created or altered {len(created)} views.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
