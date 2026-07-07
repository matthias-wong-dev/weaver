#!/usr/bin/env python3
"""Generate a Tabular XMLA/TMSL model script from a compact TOM YAML spec."""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent

RELATIONSHIP_RE = re.compile(
    r"^'(?P<from_table>[^']+)'\[(?P<from_column>[^\]]+)\]\s+"
    r"(?P<pattern>\*<-1|\*<~1|\*<->\*)\s+"
    r"'(?P<to_table>[^']+)'\[(?P<to_column>[^\]]+)\]\s*$"
)

MODEL_COLUMN_RE = re.compile(r"^'(?P<table>[^']+)'\[(?P<column>[^\]]+)\]$")

SQL_TO_TABULAR_TYPE = {
    "bit": "boolean",
    "tinyint": "int64",
    "smallint": "int64",
    "int": "int64",
    "bigint": "int64",
    "decimal": "decimal",
    "numeric": "decimal",
    "money": "decimal",
    "smallmoney": "decimal",
    "real": "double",
    "float": "double",
    "date": "dateTime",
    "datetime": "dateTime",
    "datetime2": "dateTime",
    "smalldatetime": "dateTime",
    "time": "dateTime",
    "datetimeoffset": "dateTime",
    "varchar": "string",
    "nvarchar": "string",
    "char": "string",
    "nchar": "string",
    "text": "string",
    "ntext": "string",
    "uniqueidentifier": "string",
}

RELATIONSHIP_PATTERNS = {
    "*<-1": {
        "fromCardinality": "many",
        "toCardinality": "one",
        "crossFilteringBehavior": "oneDirection",
        "isActive": True,
    },
    "*<~1": {
        "fromCardinality": "many",
        "toCardinality": "one",
        "crossFilteringBehavior": "oneDirection",
        "isActive": False,
    },
    "*<->*": {
        "fromCardinality": "many",
        "toCardinality": "many",
        "crossFilteringBehavior": "bothDirections",
        "isActive": True,
    },
}


def load_yaml(path: Path) -> dict[str, Any]:
    try:
        import yaml
    except ModuleNotFoundError as exc:
        raise SystemExit(
            "PyYAML is required. Install dependencies with: "
            ".venv/bin/python -m pip install -r requirements.txt"
        ) from exc

    text = path.read_text(encoding="utf-8")
    loaded = yaml.safe_load(quote_compact_relationships(text))
    if not isinstance(loaded, dict):
        raise ValueError(f"{path}: expected a YAML mapping at the document root")
    return loaded


def quote_compact_relationships(text: str) -> str:
    """Make bare relationship list items valid YAML before parsing."""
    lines = []
    section: str | None = None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped == "Relationships:":
            section = "relationships"
            lines.append(line)
            continue
        if stripped == "Alter columns:":
            section = "alter_columns"
            lines.append(line)
            continue
        if section and stripped and not line.startswith((" ", "\t", "-")):
            section = None
        if section == "relationships" and stripped.startswith("- '") and "*<" in stripped:
            indent = line[: len(line) - len(line.lstrip())]
            value = stripped[2:].strip()
            line = f"{indent}- {json.dumps(value)}"
        elif section == "alter_columns" and stripped.startswith("'") and "':" not in stripped:
            indent = line[: len(line) - len(line.lstrip())]
            key, separator, value = stripped.partition(":")
            if separator:
                line = f"{indent}{json.dumps(key)}:{value}"
        lines.append(line)
    return "\n".join(lines) + "\n"


def normalize_key(row: dict[str, Any], key: str) -> str | None:
    for candidate in (key, key.lower(), key.upper()):
        if candidate in row:
            value = row[candidate]
            return None if value is None else str(value)
    for candidate, value in row.items():
        if candidate.lower() == key.lower():
            return None if value is None else str(value)
    return None


def quote_sql_name(name: str) -> str:
    return "[" + name.replace("]", "]]") + "]"


def quote_sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def source_query(schema: str, table: str, columns: list[dict[str, Any]]) -> str:
    column_list = ",\n    ".join(quote_sql_name(column["name"]) for column in columns)
    return (
        "SELECT\n"
        f"    {column_list}\n"
        f"FROM {quote_sql_name(schema)}.{quote_sql_name(table)}"
    )


def read_info_schema_csv(path: Path, schema: str) -> dict[tuple[str, str], list[dict[str, Any]]]:
    by_table: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            table_schema = normalize_key(row, "TABLE_SCHEMA")
            table_name = normalize_key(row, "TABLE_NAME")
            column_name = normalize_key(row, "COLUMN_NAME")
            data_type = normalize_key(row, "DATA_TYPE")
            ordinal = normalize_key(row, "ORDINAL_POSITION")
            if not table_schema or not table_name or not column_name or not data_type:
                continue
            if table_schema.casefold() != schema.casefold():
                continue
            by_table[(table_schema, table_name)].append(
                {
                    "name": column_name,
                    "dataType": map_data_type(data_type),
                    "sourceDataType": data_type,
                    "ordinal": int(ordinal or 0),
                }
            )

    for columns in by_table.values():
        columns.sort(key=lambda column: column["ordinal"])
    return by_table


def read_info_schema_sql(connection_string: str, schema: str) -> dict[tuple[str, str], list[dict[str, Any]]]:
    try:
        import pyodbc
    except ModuleNotFoundError as exc:
        raise SystemExit(
            "pyodbc is required. Install dependencies with: "
            ".venv/bin/python -m pip install -r requirements.txt"
        ) from exc

    sql = """
select
    [TABLE_SCHEMA],
    [TABLE_NAME],
    [COLUMN_NAME],
    [ORDINAL_POSITION],
    [DATA_TYPE]
from [INFORMATION_SCHEMA].[COLUMNS]
where [TABLE_SCHEMA] = ?
order by
    [TABLE_SCHEMA],
    [TABLE_NAME],
    [ORDINAL_POSITION];
""".strip()
    by_table: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    with pyodbc.connect(connection_string, timeout=30) as conn:
        cursor = conn.cursor()
        cursor.execute(sql, schema)
        for row in cursor.fetchall():
            by_table[(row.TABLE_SCHEMA, row.TABLE_NAME)].append(
                {
                    "name": row.COLUMN_NAME,
                    "dataType": map_data_type(row.DATA_TYPE),
                    "sourceDataType": row.DATA_TYPE,
                    "ordinal": int(row.ORDINAL_POSITION),
                }
            )
    return by_table


def map_data_type(sql_type: str) -> str:
    normalized = sql_type.lower().strip()
    if normalized not in SQL_TO_TABULAR_TYPE:
        print(
            f"warning: unmapped SQL type {sql_type!r}; using string",
            file=sys.stderr,
        )
    return SQL_TO_TABULAR_TYPE.get(normalized, "string")


def parse_tables(spec: dict[str, Any]) -> dict[str, dict[str, Any]]:
    raw_tables = spec.get("Tables")
    if not isinstance(raw_tables, dict):
        raise ValueError("TOM YAML must contain a Tables mapping")

    tables: dict[str, dict[str, Any]] = {}
    for semantic_name, declaration in raw_tables.items():
        if isinstance(declaration, str):
            tables[semantic_name] = {"source": declaration, "properties": {}}
        elif isinstance(declaration, dict):
            source = declaration.get(".source")
            if not source:
                raise ValueError(f"table {semantic_name!r}: expanded declaration needs .source")
            properties = {
                key: value for key, value in declaration.items() if not str(key).startswith(".")
            }
            tables[semantic_name] = {"source": source, "properties": properties}
        else:
            raise ValueError(f"table {semantic_name!r}: unsupported table declaration")
    return tables


def find_columns(
    info_schema: dict[tuple[str, str], list[dict[str, Any]]],
    schema: str,
    table: str,
) -> list[dict[str, Any]]:
    for (table_schema, table_name), columns in info_schema.items():
        if table_schema.casefold() == schema.casefold() and table_name.casefold() == table.casefold():
            return columns
    raise ValueError(f"no information_schema columns found for {schema}.{table}")


def build_source_table(
    semantic_name: str,
    source_schema: str,
    source_table: str,
    table_properties: dict[str, Any],
    columns: list[dict[str, Any]],
    data_source_name: str,
) -> dict[str, Any]:
    table: dict[str, Any] = {
        "name": semantic_name,
        "columns": [
            {
                "name": column["name"],
                "dataType": column["dataType"],
                "sourceColumn": column["name"],
            }
            for column in columns
        ],
        "partitions": [
            {
                "name": semantic_name,
                "mode": "import",
                "source": {
                    "type": "query",
                    "dataSource": data_source_name,
                    "query": source_query(source_schema, source_table, columns),
                },
            }
        ],
    }
    table.update(table_properties)
    return table


def build_measure_table(measures: dict[str, Any]) -> dict[str, Any]:
    measure_objects = []
    for measure_name, declaration in measures.items():
        if not isinstance(declaration, dict):
            raise ValueError(f"measure {measure_name!r}: expected a mapping")
        if "expression" not in declaration:
            raise ValueError(f"measure {measure_name!r}: missing expression")

        measure = {
            "name": measure_name,
            "expression": str(declaration["expression"]).strip(),
        }
        for key in ("description", "formatString", "displayFolder", "isHidden"):
            if key in declaration:
                measure[key] = declaration[key]
        measure_objects.append(measure)

    return {
        "name": "Measure",
        "isHidden": True,
        "columns": [{"name": "Measure name", "dataType": "string", "isHidden": True}],
        "partitions": [
            {
                "name": "Measure",
                "mode": "import",
                "source": {
                    "type": "calculated",
                    "expression": 'DATATABLE("Measure name", STRING, {})',
                },
            }
        ],
        "measures": measure_objects,
    }


def parse_relationship(raw_relationship: str) -> dict[str, Any]:
    match = RELATIONSHIP_RE.match(raw_relationship)
    if not match:
        raise ValueError(f"invalid relationship declaration: {raw_relationship!r}")
    parts = match.groupdict()
    pattern = parts.pop("pattern")
    relationship = {
        "name": (
            f"{parts['from_table']}[{parts['from_column']}] -> "
            f"{parts['to_table']}[{parts['to_column']}]"
        ),
        "fromTable": parts["from_table"],
        "fromColumn": parts["from_column"],
        "toTable": parts["to_table"],
        "toColumn": parts["to_column"],
    }
    relationship.update(RELATIONSHIP_PATTERNS[pattern])
    return relationship


def apply_column_overrides(tables: list[dict[str, Any]], spec: dict[str, Any]) -> None:
    table_by_name = {table["name"]: table for table in tables}
    overrides = spec.get("Alter columns") or {}
    if not isinstance(overrides, dict):
        raise ValueError("Alter columns must be a mapping when present")

    for target, properties in overrides.items():
        match = MODEL_COLUMN_RE.match(target)
        if not match:
            raise ValueError(f"invalid column override target: {target!r}")
        table_name = match.group("table")
        column_name = match.group("column")
        table = table_by_name.get(table_name)
        if not table:
            raise ValueError(f"column override references unknown table {table_name!r}")
        column = next(
            (candidate for candidate in table.get("columns", []) if candidate["name"] == column_name),
            None,
        )
        if not column:
            raise ValueError(
                f"column override references unknown column {table_name!r}[{column_name!r}]"
            )
        if not isinstance(properties, dict):
            raise ValueError(f"column override {target!r}: expected a mapping")
        column.update(properties)


def build_tmsl(
    spec: dict[str, Any],
    info_schema: dict[tuple[str, str], list[dict[str, Any]]],
    connection_string: str,
    compatibility_level: int,
) -> dict[str, Any]:
    model_name = spec.get("Model")
    source_schema = spec.get("Data source schema")
    if not model_name or not source_schema:
        raise ValueError("TOM YAML must define Model and Data source schema")

    data_source_name = "SQL"
    source_tables = parse_tables(spec)
    tables = []
    for semantic_name, table_spec in source_tables.items():
        source_table = table_spec["source"]
        columns = find_columns(info_schema, source_schema, source_table)
        tables.append(
            build_source_table(
                semantic_name=semantic_name,
                source_schema=source_schema,
                source_table=source_table,
                table_properties=table_spec["properties"],
                columns=columns,
                data_source_name=data_source_name,
            )
        )

    measures = spec.get("Measures") or {}
    if measures:
        tables.append(build_measure_table(measures))
    apply_column_overrides(tables, spec)

    raw_relationships = spec.get("Relationships") or []
    relationships = [parse_relationship(item) for item in raw_relationships]

    return {
        "createOrReplace": {
            "object": {"database": model_name},
            "database": {
                "name": model_name,
                "compatibilityLevel": compatibility_level,
                "model": {
                    "culture": "en-US",
                    "dataSources": [
                        {
                            "type": "provider",
                            "name": data_source_name,
                            "connectionString": connection_string,
                            "impersonationMode": "impersonateServiceAccount",
                        }
                    ],
                    "tables": tables,
                    "relationships": relationships,
                },
            },
        }
    }


def build_semantic_model_execution_code(
    tmsl: dict[str, Any] | str,
    workspace: str,
    semantic_model: str,
) -> str:
    xmla_command = tmsl if isinstance(tmsl, str) else json.dumps(tmsl, indent=2)
    return "\n".join(
        [
            "import traceback",
            "import sempy.fabric as fabric",
            "",
            f"workspace = {json.dumps(workspace)}",
            f"semantic_model = {json.dumps(semantic_model)}",
            f"xmla_command = {json.dumps(xmla_command)}",
            "",
            "try:",
            "    result = fabric.execute_xmla(",
            "        dataset=semantic_model,",
            "        xmla_command=xmla_command,",
            "        workspace=workspace,",
            "        use_readwrite_connection=True,",
            "    )",
            "    print(result)",
            "except Exception:",
            "    traceback.print_exc()",
            "    raise",
            "",
        ]
    )


def execute_tmsl_via_spark_session(
    tmsl: dict[str, Any] | str,
    *,
    workspace: str,
    semantic_model: str,
    spark_workspace_id: str,
    lakehouse_id: str,
    session_id: str | None = None,
    keep_session: bool = False,
    environment_id: str | None = None,
    conf: dict[str, Any] | None = None,
    poll_interval: float = 5.0,
    timeout: float = 1200.0,
    show_json: bool = False,
) -> None:
    sys.path.insert(0, str(SCRIPT_DIR))
    import sparksession

    sessions_url = sparksession.livy_sessions_url(
        workspace_id=spark_workspace_id,
        lakehouse_id=lakehouse_id,
    )
    token = sparksession.get_access_token()
    created_session = False
    code = build_semantic_model_execution_code(
        tmsl=tmsl,
        workspace=workspace,
        semantic_model=semantic_model,
    )

    try:
        if session_id:
            livy_session_id = session_id
            session_url = f"{sessions_url}/{livy_session_id}"
        else:
            session = sparksession.create_session(
                sessions_url=sessions_url,
                token=token,
                environment_id=environment_id,
                conf=conf,
            )
            livy_session_id = str(session["id"])
            session_url = f"{sessions_url}/{livy_session_id}"
            created_session = True
            print(f"created session {livy_session_id}", file=sys.stderr, flush=True)

        sparksession.wait_for_session_idle(session_url, token, poll_interval, timeout)
        statement = sparksession.submit_statement(session_url, token, code, "pyspark")
        statement_id = str(statement["id"])
        print(f"submitted statement {statement_id}", file=sys.stderr, flush=True)
        final_statement = sparksession.wait_for_statement(
            f"{session_url}/statements/{statement_id}",
            token,
            poll_interval,
            timeout,
        )
        sparksession.print_statement_output(final_statement, show_json)
    finally:
        if created_session and not keep_session:
            try:
                sparksession.delete_session(session_url, token)
                print(f"deleted session {livy_session_id}", file=sys.stderr, flush=True)
            except Exception as exc:  # noqa: BLE001 - cleanup should not hide SemPy errors.
                print(f"failed to delete session {livy_session_id}: {exc}", file=sys.stderr)


def parse_conf_json(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise argparse.ArgumentTypeError(f"not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise argparse.ArgumentTypeError("must be a JSON object")
    return parsed


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tom_yml", type=Path, help="Path to the compact TOM YAML file")
    parser.add_argument(
        "--connection-string",
        required=True,
        help="SQL Server/Fabric connection string used for metadata and emitted datasource",
    )
    parser.add_argument(
        "--info-schema-csv",
        type=Path,
        help="Use a saved INFORMATION_SCHEMA.COLUMNS CSV instead of querying SQL",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Write generated TMSL JSON to this file instead of stdout",
    )
    parser.add_argument("--compatibility-level", type=int, default=1567)
    parser.add_argument(
        "--execute-workspace",
        help="Fabric workspace containing the destination semantic model",
    )
    parser.add_argument(
        "--execute-semantic-model",
        help="Destination semantic model name or ID passed to SemPy fabric.execute_xmla",
    )
    parser.add_argument(
        "--spark-workspace-id",
        help="Fabric workspace ID used to create the Livy Spark session",
    )
    parser.add_argument(
        "--lakehouse-id",
        help="Fabric lakehouse ID used to create the Livy Spark session",
    )
    parser.add_argument("--session-id", help="Reuse an existing Livy session")
    parser.add_argument("--keep-session", action="store_true")
    parser.add_argument("--environment-id")
    parser.add_argument("--conf-json", type=parse_conf_json, default={})
    parser.add_argument("--poll-interval", type=float, default=5.0)
    parser.add_argument("--timeout", type=float, default=1200.0)
    parser.add_argument("--show-spark-json", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    spec = load_yaml(args.tom_yml)
    source_schema = spec.get("Data source schema")
    if not source_schema:
        raise ValueError("TOM YAML must define Data source schema")

    if args.info_schema_csv:
        info_schema = read_info_schema_csv(args.info_schema_csv, source_schema)
    else:
        info_schema = read_info_schema_sql(args.connection_string, source_schema)

    tmsl = build_tmsl(
        spec=spec,
        info_schema=info_schema,
        connection_string=args.connection_string,
        compatibility_level=args.compatibility_level,
    )
    output = json.dumps(tmsl, indent=2) + "\n"
    if args.output:
        args.output.write_text(output, encoding="utf-8")
    else:
        print(output, end="")

    if args.execute_workspace or args.execute_semantic_model:
        missing = [
            name
            for name, value in {
                "--execute-workspace": args.execute_workspace,
                "--execute-semantic-model": args.execute_semantic_model,
                "--spark-workspace-id": args.spark_workspace_id,
                "--lakehouse-id": args.lakehouse_id,
            }.items()
            if not value
        ]
        if missing:
            raise ValueError(f"execution requires: {', '.join(missing)}")
        execute_tmsl_via_spark_session(
            tmsl=tmsl,
            workspace=args.execute_workspace,
            semantic_model=args.execute_semantic_model,
            spark_workspace_id=args.spark_workspace_id,
            lakehouse_id=args.lakehouse_id,
            session_id=args.session_id,
            keep_session=args.keep_session,
            environment_id=args.environment_id,
            conf=args.conf_json,
            poll_interval=args.poll_interval,
            timeout=args.timeout,
            show_json=args.show_spark_json,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
