#!/usr/bin/env python3
"""Materialize SES views to sibling _Data tables and record timings."""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from sqlserver import connect


DEFAULT_SES_DIR = Path(os.environ.get("SES_DIR", "SES"))
TABLE_ID_RE = re.compile(r"View ID:\s*([A-Za-z_][\w]*)\.([A-Za-z_][\w]*)")


@dataclass(frozen=True)
class ViewRef:
    schema: str
    view: str
    path: Path

    @property
    def name(self) -> str:
        return f"{self.schema}.{self.view}"

    def materialized_name(self, suffix: str) -> str:
        return f"{self.schema}.{self.view}{suffix}"


def quote_name(name: str) -> str:
    return "[" + name.replace("]", "]]") + "]"


def qualified_name(schema: str, name: str) -> str:
    return f"{quote_name(schema)}.{quote_name(name)}"


def read_view_ref(path: Path) -> ViewRef:
    text = path.read_text(encoding="utf-8")
    match = TABLE_ID_RE.search(text)
    if not match:
        raise ValueError(f"{path}: missing View ID metadata")
    schema, view = match.groups()
    return ViewRef(schema=schema, view=view, path=path)


def view_refs(ses_dir: Path) -> list[ViewRef]:
    return [read_view_ref(path) for path in sorted(ses_dir.glob("*.sql"))]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suffix", default="_Data")
    parser.add_argument("--label", default="baseline")
    parser.add_argument("--only-schema", action="append", default=[])
    parser.add_argument("--only-view", action="append", default=[])
    parser.add_argument("--skip-existing", action="store_true")
    parser.add_argument("--query-timeout", type=int, default=0)
    parser.add_argument("--ses-dir", type=Path, default=DEFAULT_SES_DIR)
    return parser.parse_args()


def ensure_diagnostics(cursor) -> None:
    cursor.execute("IF SCHEMA_ID(N'Diagnostics') IS NULL EXEC(N'CREATE SCHEMA [Diagnostics]')")
    cursor.execute(
        """
        IF OBJECT_ID(N'[Diagnostics].[ViewMaterializationRun]', N'U') IS NULL
        CREATE TABLE [Diagnostics].[ViewMaterializationRun] (
              [Run ID] varchar(36) NOT NULL
            , [Label] varchar(100) NOT NULL
            , [Started at UTC] datetime2(6) NOT NULL
            , [Finished at UTC] datetime2(6) NULL
            , [Succeeded] bit NULL
        )
        """
    )
    cursor.execute(
        """
        IF OBJECT_ID(N'[Diagnostics].[ViewMaterializationTiming]', N'U') IS NULL
        CREATE TABLE [Diagnostics].[ViewMaterializationTiming] (
              [Run ID] varchar(36) NOT NULL
            , [Schema] varchar(128) NOT NULL
            , [View] varchar(128) NOT NULL
            , [Materialized table] varchar(300) NOT NULL
            , [Started at UTC] datetime2(6) NOT NULL
            , [Finished at UTC] datetime2(6) NULL
            , [Drop seconds] decimal(19, 3) NULL
            , [CTAS seconds] decimal(19, 3) NULL
            , [Count seconds] decimal(19, 3) NULL
            , [Total seconds] decimal(19, 3) NULL
            , [Row count] bigint NULL
            , [Succeeded] bit NOT NULL
            , [Error] varchar(8000) NULL
        )
        """
    )


def table_exists(cursor, schema: str, table: str) -> bool:
    cursor.execute(
        """
        SELECT CASE WHEN OBJECT_ID(?, N'U') IS NULL THEN 0 ELSE 1 END
        """,
        f"{qualified_name(schema, table)}",
    )
    return bool(cursor.fetchone()[0])


def insert_run(cursor, run_id: str, label: str, started_at: datetime) -> None:
    cursor.execute(
        """
        INSERT INTO [Diagnostics].[ViewMaterializationRun]
            ([Run ID], [Label], [Started at UTC])
        VALUES (?, ?, ?)
        """,
        run_id,
        label,
        started_at.replace(tzinfo=None),
    )


def finish_run(cursor, run_id: str, succeeded: bool) -> None:
    cursor.execute(
        """
        UPDATE [Diagnostics].[ViewMaterializationRun]
        SET [Finished at UTC] = ?, [Succeeded] = ?
        WHERE [Run ID] = ?
        """,
        datetime.now(UTC).replace(tzinfo=None),
        int(succeeded),
        run_id,
    )


def insert_timing(
    cursor,
    run_id: str,
    ref: ViewRef,
    suffix: str,
    started_at: datetime,
    finished_at: datetime,
    drop_seconds: float | None,
    ctas_seconds: float | None,
    count_seconds: float | None,
    row_count: int | None,
    succeeded: bool,
    error: str | None,
) -> None:
    total_seconds = (finished_at - started_at).total_seconds()
    cursor.execute(
        """
        INSERT INTO [Diagnostics].[ViewMaterializationTiming] (
              [Run ID]
            , [Schema]
            , [View]
            , [Materialized table]
            , [Started at UTC]
            , [Finished at UTC]
            , [Drop seconds]
            , [CTAS seconds]
            , [Count seconds]
            , [Total seconds]
            , [Row count]
            , [Succeeded]
            , [Error]
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        run_id,
        ref.schema,
        ref.view,
        ref.materialized_name(suffix),
        started_at.replace(tzinfo=None),
        finished_at.replace(tzinfo=None),
        drop_seconds,
        ctas_seconds,
        count_seconds,
        total_seconds,
        row_count,
        int(succeeded),
        error[:8000] if error else None,
    )


def materialize_view(cursor, ref: ViewRef, suffix: str, skip_existing: bool) -> tuple[float | None, float, float, int]:
    target = f"{ref.view}{suffix}"
    target_name = qualified_name(ref.schema, target)
    source_name = qualified_name(ref.schema, ref.view)

    if skip_existing and table_exists(cursor, ref.schema, target):
        started = time.monotonic()
        cursor.execute(f"SELECT COUNT_BIG(*) FROM {target_name}")
        row_count = int(cursor.fetchone()[0])
        return None, 0.0, time.monotonic() - started, row_count

    started = time.monotonic()
    cursor.execute(f"DROP TABLE IF EXISTS {target_name}")
    drop_seconds = time.monotonic() - started

    started = time.monotonic()
    cursor.execute(f"CREATE TABLE {target_name} AS SELECT * FROM {source_name}")
    ctas_seconds = time.monotonic() - started

    started = time.monotonic()
    cursor.execute(f"SELECT COUNT_BIG(*) FROM {target_name}")
    row_count = int(cursor.fetchone()[0])
    count_seconds = time.monotonic() - started

    return drop_seconds, ctas_seconds, count_seconds, row_count


def filtered_refs(args: argparse.Namespace) -> list[ViewRef]:
    refs = view_refs(args.ses_dir.resolve())
    only_schemas = {schema.lower() for schema in args.only_schema}
    only_views = {view.lower() for view in args.only_view}
    if only_schemas:
        refs = [ref for ref in refs if ref.schema.lower() in only_schemas]
    if only_views:
        refs = [ref for ref in refs if ref.name.lower() in only_views or ref.view.lower() in only_views]
    return refs


def main() -> int:
    args = parse_args()
    refs = filtered_refs(args)
    if not refs:
        print("No views matched.", file=sys.stderr)
        return 2

    run_id = str(uuid.uuid4())
    run_started = datetime.now(UTC)

    with connect() as conn:
        conn.autocommit = True
        cursor = conn.cursor()
        try:
            cursor.timeout = args.query_timeout
        except AttributeError:
            pass
        ensure_diagnostics(cursor)
        insert_run(cursor, run_id, args.label, run_started)

        succeeded = True
        try:
            for index, ref in enumerate(refs, start=1):
                started_at = datetime.now(UTC)
                print(f"[{index}/{len(refs)}] {ref.name} -> {ref.materialized_name(args.suffix)}", flush=True)
                try:
                    drop_seconds, ctas_seconds, count_seconds, row_count = materialize_view(
                        cursor,
                        ref,
                        args.suffix,
                        args.skip_existing,
                    )
                    finished_at = datetime.now(UTC)
                    insert_timing(
                        cursor,
                        run_id,
                        ref,
                        args.suffix,
                        started_at,
                        finished_at,
                        drop_seconds,
                        ctas_seconds,
                        count_seconds,
                        row_count,
                        True,
                        None,
                    )
                    print(
                        "    "
                        f"rows={row_count} "
                        f"drop={drop_seconds or 0:.3f}s "
                        f"ctas={ctas_seconds:.3f}s "
                        f"count={count_seconds:.3f}s "
                        f"total={(finished_at - started_at).total_seconds():.3f}s",
                        flush=True,
                    )
                except Exception as exc:  # noqa: BLE001 - record and continue.
                    succeeded = False
                    finished_at = datetime.now(UTC)
                    insert_timing(
                        cursor,
                        run_id,
                        ref,
                        args.suffix,
                        started_at,
                        finished_at,
                        None,
                        None,
                        None,
                        None,
                        False,
                        str(exc),
                    )
                    print(f"    ERROR: {exc}", file=sys.stderr, flush=True)
        finally:
            finish_run(cursor, run_id, succeeded)

    print(f"run_id={run_id}", flush=True)
    return 0 if succeeded else 1


if __name__ == "__main__":
    raise SystemExit(main())
