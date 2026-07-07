#!/usr/bin/env python3
"""Generate a Power BI Project semantic model folder from a compact TOM YAML spec."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import generate_xmla_from_tom_yml as tom_xmla


PBISM_SCHEMA = (
    "https://developer.microsoft.com/json-schemas/fabric/item/"
    "semanticModel/definitionProperties/1.0.0/schema.json"
)


def safe_project_name(name: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', " ", name).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned or "Power BI Model"


def build_connection_string(server: str, database: str | None = None) -> str:
    parts = [
        "Driver={ODBC Driver 18 for SQL Server}",
        f"Server=tcp:{server},1433",
        "Encrypt=yes",
        "TrustServerCertificate=no",
    ]
    if database:
        parts.insert(1, f"Database={database}")
    return ";".join(parts) + ";"


def connection_string_value(connection_string: str, key: str) -> str | None:
    for part in connection_string.split(";"):
        if "=" not in part:
            continue
        part_key, value = part.split("=", 1)
        if part_key.strip().casefold() == key.casefold():
            return value.strip()
    return None


def clean_sql_server_name(server: str | None) -> str | None:
    if not server:
        return None
    server = server.strip()
    if server.lower().startswith("tcp:"):
        server = server[4:]
    if "," in server:
        server = server.split(",", 1)[0]
    return server


def escape_m_text(value: str) -> str:
    return (
        value.replace('"', '""')
        .replace("\r\n", "#(cr,lf)")
        .replace("\n", "#(lf)")
        .replace("\r", "#(cr)")
    )


def power_query_m(server: str, database: str, query: str) -> list[str]:
    escaped_query = escape_m_text(query)
    return [
        "let",
        f'    Source = Sql.Database("{server}", "{database}", [Query="{escaped_query}"])',
        "in",
        "    Source",
    ]


def convert_partitions_to_m(database: dict[str, Any], server: str, sql_database: str) -> None:
    model = database.get("model", {})
    model.pop("dataSources", None)
    for table in model.get("tables", []):
        for partition in table.get("partitions", []):
            source = partition.get("source", {})
            if source.get("type") != "query":
                continue
            partition["source"] = {
                "type": "m",
                "expression": power_query_m(server, sql_database, source["query"]),
            }


def database_from_tmsl(tmsl: dict[str, Any]) -> dict[str, Any]:
    database = tmsl["createOrReplace"]["database"]
    database.setdefault("annotations", [])
    model = database.setdefault("model", {})
    model.setdefault("annotations", [])
    model.setdefault("sourceQueryCulture", "en-US")
    model.setdefault("cultures", [])
    model.setdefault(
        "dataAccessOptions",
        {
            "legacyRedirects": True,
            "returnErrorValuesAsNull": True,
        },
    )
    model.setdefault("defaultPowerBIDataSourceVersion", "powerBI_V3")
    model.setdefault("discourageImplicitMeasures", True)
    model.setdefault("expressions", [])
    model.setdefault("functions", [])
    model.setdefault("perspectives", [])
    model.setdefault("roles", [])
    return database


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def generate_pbip_project(
    *,
    tom_yml: Path,
    output_dir: Path,
    connection_string: str,
    server: str | None,
    database: str | None,
    info_schema_csv: Path | None,
    compatibility_level: int,
    project_name: str | None = None,
    write_shortcut: bool = True,
) -> Path:
    spec = tom_xmla.load_yaml(tom_yml)
    model_name = str(spec.get("Model") or project_name or tom_yml.stem)
    source_schema = spec.get("Data source schema")
    if not source_schema:
        raise ValueError("TOM YAML must define Data source schema")

    if info_schema_csv:
        info_schema = tom_xmla.read_info_schema_csv(info_schema_csv, source_schema)
    else:
        info_schema = tom_xmla.read_info_schema_sql(connection_string, source_schema)

    tmsl = tom_xmla.build_tmsl(
        spec=spec,
        info_schema=info_schema,
        connection_string=connection_string,
        compatibility_level=compatibility_level,
    )
    project_display_name = safe_project_name(project_name or model_name)
    project_dir = output_dir / project_display_name
    semantic_model_dir = project_dir / f"{project_display_name}.SemanticModel"
    semantic_model_dir.mkdir(parents=True, exist_ok=True)

    write_json(
        semantic_model_dir / "definition.pbism",
        {
            "$schema": PBISM_SCHEMA,
            "version": "5.0",
            "settings": {
                "qnaEnabled": False,
            },
        },
    )
    database_model = database_from_tmsl(tmsl)
    m_server = clean_sql_server_name(server or connection_string_value(connection_string, "Server"))
    m_database = database or connection_string_value(connection_string, "Database")
    if m_server and m_database:
        convert_partitions_to_m(database_model, m_server, m_database)
    write_json(semantic_model_dir / "model.bim", database_model)
    (project_dir / ".gitignore").write_text(
        "**/.pbi/localSettings.json\n**/.pbi/cache.abf\n",
        encoding="utf-8",
    )
    if write_shortcut:
        write_json(
            project_dir / f"{project_display_name}.pbip",
            {
                "$schema": (
                    "https://raw.githubusercontent.com/microsoft/powerbi-desktop-samples/"
                    "main/item-schemas/common/pbip-1.0.json"
                ),
                "version": "1.0",
                "artifacts": [],
                "settings": {
                    "enableAutoRecovery": True,
                },
            },
        )
    return project_dir


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tom_yml", type=Path, help="Path to the compact TOM YAML file")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("analysis_outputs/pbip"),
        help="Directory where the PBIP project folder is written",
    )
    parser.add_argument("--project-name", help="Override the PBIP project/model folder name")
    parser.add_argument("--connection-string", help="Datasource connection string for model.bim")
    parser.add_argument("--server", default=os.environ.get("SQLSERVER_HOST"))
    parser.add_argument("--database", default=os.environ.get("SQLSERVER_DATABASE"))
    parser.add_argument(
        "--info-schema-csv",
        type=Path,
        help="Use a saved INFORMATION_SCHEMA.COLUMNS CSV instead of querying SQL",
    )
    parser.add_argument("--compatibility-level", type=int, default=1567)
    parser.add_argument(
        "--no-pbip-shortcut",
        action="store_true",
        help="Only write the .SemanticModel folder and .gitignore",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    connection_string = args.connection_string
    if not connection_string:
        if not args.server:
            print("Provide --connection-string or --server.", file=sys.stderr)
            return 2
        connection_string = build_connection_string(args.server, args.database)

    try:
        project_dir = generate_pbip_project(
            tom_yml=args.tom_yml,
            output_dir=args.output_dir,
            connection_string=connection_string,
            server=args.server,
            database=args.database,
            info_schema_csv=args.info_schema_csv,
            compatibility_level=args.compatibility_level,
            project_name=args.project_name,
            write_shortcut=not args.no_pbip_shortcut,
        )
    except Exception as exc:  # noqa: BLE001 - CLI should show generator errors.
        print(f"PBIP generation failed: {exc}", file=sys.stderr)
        return 1

    print(project_dir, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
