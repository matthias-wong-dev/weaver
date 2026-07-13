"""Real SQL build/load backend for the database-representation subsystem.

Build executes DDL layer by layer using the dbrep dependency graph (no retry
loops). Independent objects within a layer run in parallel, bounded by the SQL
server's ``degrees_of_parallelism``. For each Table object it installs a
self-inferring backing table + view and a load stored procedure; for each View
object it installs the view. Shared ``_`` and ``_weaver`` schemas are created
serially before any parallel object work. A ``_weaver.objects`` metadata table
inside the target database records managed objects so ``--prune`` can drop only
removed managed objects. Load executes installed load procedures in dependency
order.

The backend is self-contained: it reads only the SQL SES source objects and its
own in-database metadata table. It does not depend on any Lakehouse runtime.
"""

from __future__ import annotations

import hashlib
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from itertools import groupby
from typing import Callable, Iterable, Sequence

from ..errors import BuildError, LoadError
from ..ses.discovery import SQL as SQL_LANGUAGE
from ..ses.graph import topological_layers
from ..ses.metadata import TABLE, VIEW
from .connection import connect, execute_script, query
from .ddl import (
    _derive_table_names,
    generate_infer_create_table_sql,
    wrap_create_or_alter_view,
)
from .etl import generate_load_stored_procedure_sql

MANAGED_SCHEMA = "_weaver"
LOAD_PROCEDURE_SCHEMA = "_"
MANIFEST_TABLE = "_weaver.objects"

_ENSURE_MANIFEST_TABLE_SQL = f"""
if object_id(N'{MANIFEST_TABLE}', N'U') is null
create table {MANIFEST_TABLE} (
    object_id varchar(400) not null,
    kind varchar(20) not null,
    schema_name varchar(200) not null,
    object_name varchar(200) not null,
    view_name varchar(400) not null,
    current_table varchar(400) null,
    load_procedure varchar(400) null,
    source_hash varchar(64) not null,
    layer_index int not null,
    installed_at datetime2(6) not null
);
""".strip()


@dataclass
class SqlBuildResult:
    target: str
    server: str
    database: str
    schemas: tuple[str, ...]
    tables: tuple[str, ...]
    views: tuple[str, ...]
    procedures: tuple[str, ...]
    pruned: tuple[str, ...]
    layers: tuple[tuple[str, ...], ...]


@dataclass
class SqlLoadResult:
    target: str
    server: str
    database: str
    executed: tuple[str, ...]


@dataclass
class SqlWipeResult:
    target: str
    server: str
    database: str
    before: dict
    after: dict


def wipe_sql_target(target) -> SqlWipeResult:
    """Drop all user views, tables, functions, procedures, and (best-effort) schemas.

    Objects are dropped in one committed batch; user schemas are then dropped
    individually and best-effort, since some (e.g. Fabric-internal ``_rsc``) are
    protected and cannot be removed.
    """

    from .wrangle import get_sql_template

    script = get_sql_template("admin/wipe")
    with connect(target.host, target.database) as conn:
        before = _object_counts(conn)
        execute_script(conn, script)
        _drop_user_schemas(conn)
        after = _object_counts(conn)
    return SqlWipeResult(
        target=target.alias,
        server=target.host,
        database=target.database,
        before=before,
        after=after,
    )


def _drop_user_schemas(conn) -> None:
    schemas = query(
        conn,
        "select name from sys.schemas "
        "where lower(name) not in (N'dbo', N'guest', N'information_schema', N'sys', N'queryinsights') "
        "and schema_id < 16384",
    )
    for row in schemas:
        name = row["name"].replace("]", "]]")
        try:
            execute_script(conn, f"drop schema [{name}];")
        except Exception:
            pass  # Fabric-internal schemas cannot be dropped; leave them.


def _object_counts(conn) -> dict:
    rows = query(
        conn,
        "select type_desc as kind, count(*) as n from sys.objects "
        "where lower(schema_name(schema_id)) not in (N'guest', N'information_schema', N'sys', N'queryinsights') "
        "and type in (N'U', N'V', N'P', N'FN', N'IF', N'TF', N'FS', N'FT') "
        "group by type_desc",
    )
    return {row["kind"]: row["n"] for row in rows}


# Fabric occasionally drops a connection mid-batch (08S01 communication link
# failure, or an "in-flight system update" during connect). All build/load
# operations here are idempotent (create-or-alter, if-not-exists, delete+insert),
# so retrying transient failures is safe.
_TRANSIENT_MARKERS = (
    "08s01",
    "08001",
    "communication link failure",
    "system update",
    "hyt00",
    "hyt01",
)


def _is_transient(exc: Exception) -> bool:
    return any(marker in str(exc).lower() for marker in _TRANSIENT_MARKERS)


def _with_retry(operation, *, attempts: int = 4, delay: float = 5.0):
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            return operation()
        except Exception as exc:  # noqa: BLE001 - retried only when transient
            if not _is_transient(exc) or attempt == attempts - 1:
                raise
            last = exc
            time.sleep(delay)
    raise last  # pragma: no cover - loop always returns or raises


def _run_sql_with_retry(target, sql: str) -> None:
    def operation() -> None:
        with connect(target.host, target.database) as conn:
            execute_script(conn, sql)

    _with_retry(operation)


def build_sql_target(
    objects: Sequence,
    target,
    *,
    prune: bool = False,
    degrees_of_parallelism: int | None = None,
) -> SqlBuildResult:
    """Build all objects of one SQL target, layer by layer with bounded parallelism."""

    objects = list(objects)
    if not objects:
        return SqlBuildResult(target.alias, target.host, target.database, (), (), (), (), (), ())

    for planned in objects:
        if planned.source.language != SQL_LANGUAGE:
            raise BuildError(
                f"SQL target {target.alias!r} requires .sql source objects; "
                f"{planned.id} is {planned.source.language!r}"
            )

    dop = degrees_of_parallelism or target.degrees_of_parallelism or 1
    by_id = {planned.id: planned for planned in objects}
    ids = set(by_id)
    edges = [
        (dependency.id, planned.id)
        for planned in objects
        for dependency in planned.dependencies
        if dependency.id in ids
    ]
    layers = topological_layers(ids, edges)
    layer_index = {object_id: index for index, layer in enumerate(layers) for object_id in layer}

    schemas = sorted(
        {planned.source.metadata.object_id.schema for planned in objects}
        | {LOAD_PROCEDURE_SCHEMA, MANAGED_SCHEMA}
    )

    def _setup():
        with connect(target.host, target.database) as conn:
            for schema in schemas:
                execute_script(conn, _ensure_schema_sql(schema))
            execute_script(conn, _ENSURE_MANIFEST_TABLE_SQL)
            return _read_manifest(conn)

    previous = _with_retry(_setup)

    for layer in layers:
        _run_parallel(dop, layer, lambda object_id: _install_object_ddl(target, by_id[object_id]))

    table_ids = sorted(planned.id for planned in objects if planned.kind == TABLE)
    _run_parallel(dop, table_ids, lambda object_id: _install_load_procedure(target, by_id[object_id]))

    def _write_metadata():
        with connect(target.host, target.database) as conn:
            for planned in objects:
                _upsert_manifest_row(conn, planned, layer_index[planned.id])
            return _prune(conn, previous, ids) if prune else ()

    pruned: tuple[str, ...] = _with_retry(_write_metadata)

    return SqlBuildResult(
        target=target.alias,
        server=target.host,
        database=target.database,
        schemas=tuple(schemas),
        tables=tuple(table_ids),
        views=tuple(sorted(planned.id for planned in objects if planned.kind == VIEW)),
        procedures=tuple(table_ids),
        pruned=pruned,
        layers=tuple(tuple(layer) for layer in layers),
    )


def load_sql_target(
    target,
    *,
    object_filter: Iterable[str] | None = None,
    degrees_of_parallelism: int | None = None,
) -> SqlLoadResult:
    """Execute installed load procedures in dependency (layer) order."""

    dop = degrees_of_parallelism or target.degrees_of_parallelism or 1
    with connect(target.host, target.database) as conn:
        if not _manifest_exists(conn):
            raise LoadError(
                f"no installed Weaver metadata in {target.database!r}; build the SQL target first"
            )
        rows = query(
            conn,
            f"select object_id, load_procedure, layer_index from {MANIFEST_TABLE} "
            "where kind = N'Table' and load_procedure is not null "
            "order by layer_index, object_id",
        )

    if object_filter is not None:
        wanted = set(object_filter)
        rows = [row for row in rows if row["object_id"] in wanted]

    executed: list[str] = []
    for _, layer_rows in groupby(rows, key=lambda row: row["layer_index"]):
        layer = list(layer_rows)
        _run_parallel(dop, layer, lambda row: _exec_procedure(target, row["load_procedure"]))
        executed.extend(row["object_id"] for row in layer)

    return SqlLoadResult(
        target=target.alias,
        server=target.host,
        database=target.database,
        executed=tuple(executed),
    )


# --- object installs -------------------------------------------------------


def _install_object_ddl(target, planned) -> None:
    name = planned.declared_as
    primary_key = list(planned.source.metadata.primary_key)
    if planned.kind == TABLE:
        sql = generate_infer_create_table_sql(
            planned.source.sql_body, name, primary_key_columns=primary_key
        )
    elif planned.kind == VIEW:
        sql = wrap_create_or_alter_view(planned.source.sql_body, name)
    else:
        raise BuildError(f"unsupported SQL object kind: {planned.kind}")
    _run_sql_with_retry(target, sql)


def _install_load_procedure(target, planned) -> None:
    sql = generate_load_stored_procedure_sql(
        planned.source.sql_body,
        planned.declared_as,
        primary_key_columns=list(planned.source.metadata.primary_key),
        is_incremental=planned.source.metadata.is_incremental,
    )
    _run_sql_with_retry(target, sql)


def _exec_procedure(target, procedure_name: str) -> None:
    _run_sql_with_retry(target, f"exec {procedure_name};")


# --- metadata table --------------------------------------------------------


def _manifest_exists(conn) -> bool:
    rows = query(conn, f"select 1 as present from sys.objects where object_id = object_id(N'{MANIFEST_TABLE}')")
    return bool(rows)


def _read_manifest(conn) -> dict:
    rows = query(
        conn,
        "select object_id, kind, schema_name, object_name, view_name, current_table, "
        f"load_procedure from {MANIFEST_TABLE}",
    )
    return {row["object_id"]: row for row in rows}


def _upsert_manifest_row(conn, planned, layer_index: int) -> None:
    names = _derive_table_names(planned.declared_as)
    schema = planned.source.metadata.object_id.schema
    object_name = planned.source.metadata.object_id.object
    is_table = planned.kind == TABLE

    cursor = conn.cursor()
    cursor.execute(f"delete from {MANIFEST_TABLE} where object_id = ?", planned.id)
    cursor.execute(
        f"insert into {MANIFEST_TABLE} (object_id, kind, schema_name, object_name, "
        "view_name, current_table, load_procedure, source_hash, layer_index, installed_at) "
        "values (?, ?, ?, ?, ?, ?, ?, ?, ?, sysutcdatetime())",
        planned.id,
        planned.kind,
        schema,
        object_name,
        names.view_name,
        names.current_table if is_table else None,
        names.load_procedure if is_table else None,
        _source_hash(planned),
        layer_index,
    )
    conn.commit()


def _prune(conn, previous: dict, current_ids: set) -> tuple[str, ...]:
    removed = [row for object_id, row in previous.items() if object_id not in current_ids]
    for row in removed:
        execute_script(conn, _drop_object_sql(row))
        cursor = conn.cursor()
        cursor.execute(f"delete from {MANIFEST_TABLE} where object_id = ?", row["object_id"])
        conn.commit()
    return tuple(sorted(row["object_id"] for row in removed))


def _drop_object_sql(row: dict) -> str:
    names = _derive_table_names(f"{row['schema_name']}.{row['object_name']}")
    statements: list[str] = []
    if row.get("load_procedure"):
        statements.append(_drop_if_exists("P", names.load_procedure, "procedure"))
    statements.append(_drop_if_exists("V", names.view_name, "view"))
    if row.get("current_table"):
        for table in (
            names.current_table,
            names.history_table,
            names.staging_table,
            names.upsert_table,
            names.reject_table,
        ):
            statements.append(_drop_if_exists("U", table, "table"))
    return "\n".join(statements)


def _drop_if_exists(object_type: str, name: str, keyword: str) -> str:
    literal = name.replace("'", "''")
    return f"if object_id(N'{literal}', N'{object_type}') is not null drop {keyword} {name};"


# --- helpers ---------------------------------------------------------------


def _ensure_schema_sql(schema: str) -> str:
    literal = schema.replace("'", "''")
    quoted = "[" + schema.replace("]", "]]") + "]"
    return f"if schema_id(N'{literal}') is null exec(N'create schema {quoted}');"


def _source_hash(planned) -> str:
    return hashlib.sha256(planned.source.text.encode("utf-8")).hexdigest()


def _run_parallel(dop: int, items: Sequence, function: Callable) -> None:
    items = list(items)
    if not items:
        return
    workers = max(1, min(int(dop), len(items)))
    if workers == 1:
        for item in items:
            function(item)
        return
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(function, item): item for item in items}
        errors = [
            (futures[future], future.exception())
            for future in as_completed(futures)
            if future.exception() is not None
        ]
    if errors:
        item, exc = errors[0]
        raise BuildError(f"SQL build/load step failed for {item!r}: {exc}") from exc
