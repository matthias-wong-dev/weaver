"""Delta materialisation layout, driven by the generic_ses fixture model.

Local Lakehouse hosts co-locate databases, so Delta tables materialise at
``Tables/<database>/<schema>/<object>``. A Fabric Lakehouse *is* the database
host, so its Delta tables omit the database: ``Tables/<schema>/<object>``. Schema
and object are always separate path components, never one dotted directory.
Logical object IDs and dependency keys are unchanged.
"""

from __future__ import annotations

import json
from pathlib import Path

from dbrep_helpers import load_config, resolve, write_config_files
from weaver_runtime.dbrep.build import BuildPair, BuildRequest, plan_build
from weaver_runtime.dbrep.build.runtime_bundle import install_build
from weaver_runtime.dbrep.lakehouse.artifacts import generate_lakehouse_artifacts

FIXTURE_SES = Path(__file__).resolve().parent / "fixtures" / "generic_ses" / "SES"

_ALL_DELTA = {
    "T1.Stage.Record": "Stage/Record",
    "T1.Mart.RecordAudit": "Mart/RecordAudit",
    "T1.Mart.RecordSnapshot": "Mart/RecordSnapshot",
    "T1.Mart.RecordCurrentAuto": "Mart/RecordCurrentAuto",
    "T1.Mart.RecordCurrentKeep": "Mart/RecordCurrentKeep",
    "T2.Mart.RecordAggregate": "Mart/RecordAggregate",
    "T3.Report.RecordSummary": "Report/RecordSummary",
}


def _no_dotted_components(materialisation: str) -> bool:
    return all("." not in part for part in materialisation.split("/"))


def _plan(weaver_path, pairs):
    config = load_config(weaver_path)
    return plan_build(
        BuildRequest(pairs=tuple(BuildPair(resolve(config, s), resolve(config, t)) for s, t in pairs))
    )


# --- C. Local build plan + catalogue ----------------------------------------


def test_local_catalogue_uses_separate_schema_object_paths(tmp_path: Path) -> None:
    servers = {"SES_Repo": {"server": str(FIXTURE_SES)}, "Lake": {"server": str(tmp_path / "lake")}}
    databases = {
        "T0_SES": {"type": "SES", "server": "SES_Repo", "database": "T0"},
        "T1_SES": {"type": "SES", "server": "SES_Repo", "database": "T1"},
        "T2_SES": {"type": "SES", "server": "SES_Repo", "database": "T2"},
        "T3_SES": {"type": "SES", "server": "SES_Repo", "database": "T3"},
        "T0_FILES": {"type": "Files", "server": "Lake", "database": "T0"},
        "T1_DELTA": {"type": "Delta", "server": "Lake", "database": "T1"},
        "T2_DELTA": {"type": "Delta", "server": "Lake", "database": "T2"},
        "T3_DELTA": {"type": "Delta", "server": "Lake", "database": "T3"},
    }
    weaver = write_config_files(tmp_path, servers, databases)
    plan = _plan(
        weaver,
        [("T0_SES", "T0_FILES"), ("T1_SES", "T1_DELTA"), ("T2_SES", "T2_DELTA"), ("T3_SES", "T3_DELTA")],
    )

    # Logical IDs (and thus the plan order keys) are unchanged.
    by_id = {obj.id: obj for obj in plan.objects}
    assert set(by_id) == {"T0.Raw.Drop", *_ALL_DELTA}

    # Local Delta materialisations carry the database and split schema/object.
    for object_id, tail in _ALL_DELTA.items():
        database = object_id.split(".", 1)[0]
        assert by_id[object_id].materialisation == f"Tables/{database}/{tail}"
    assert by_id["T0.Raw.Drop"].materialisation == "Files/T0/Raw/Drop"  # Files unchanged

    install_build(plan)
    runtime = tmp_path / "lake" / "Files" / "_weaver" / "runtime"

    catalogue = json.loads((runtime / "catalogue.json").read_text(encoding="utf-8"))
    materialisation = {entry["id"]: entry["materialisation"] for entry in catalogue["objects"]}
    assert materialisation["T1.Stage.Record"] == "Tables/T1/Stage/Record"
    assert materialisation["T2.Mart.RecordAggregate"] == "Tables/T2/Mart/RecordAggregate"
    assert materialisation["T3.Report.RecordSummary"] == "Tables/T3/Report/RecordSummary"
    assert all(_no_dotted_components(m) for m in materialisation.values())

    # The dependency graph continues to use logical IDs.
    dependency = json.loads((runtime / "load_dependency.json").read_text(encoding="utf-8"))
    assert dependency["objects"]["T1.Stage.Record"] == ["T0.Raw.Drop"]
    assert dependency["objects"]["T2.Mart.RecordAggregate"] == ["T1.Stage.Record"]
    assert dependency["objects"]["T3.Report.RecordSummary"] == ["T2.Mart.RecordAggregate"]


# --- F. Fabric artifact (no live Fabric) ------------------------------------


def test_fabric_artifact_delta_paths_omit_database(tmp_path: Path) -> None:
    servers = {
        "SES_Repo": {"server": str(FIXTURE_SES)},
        "Fabric": {"type": "Fabric Lakehouse", "server": "Workspace/T1"},
    }
    databases = {
        "T0_SES": {"type": "SES", "server": "SES_Repo", "database": "T0"},
        "T1_SES": {"type": "SES", "server": "SES_Repo", "database": "T1"},
        "T0_FILES": {"type": "Files", "server": "Fabric", "database": "T0"},
        "T1_DELTA": {"type": "Delta", "server": "Fabric", "database": "T1"},
    }
    weaver = write_config_files(tmp_path, servers, databases)
    plan = _plan(weaver, [("T0_SES", "T0_FILES"), ("T1_SES", "T1_DELTA")])

    [artifact] = generate_lakehouse_artifacts(plan, tmp_path / "gen")

    program = artifact.build_program_path.read_text(encoding="utf-8")
    assert "Tables/Stage/Record" in program
    assert "Tables/Mart/RecordAudit" in program
    assert "Tables/T1/" not in program  # database dir omitted on Fabric

    doc = json.loads(artifact.plan_path.read_text(encoding="utf-8"))
    assert "Tables/Stage/Record" in doc["delta_tables"]
    assert "Tables/Mart/RecordAudit" in doc["delta_tables"]
    assert all(_no_dotted_components(path) for path in doc["delta_tables"])
    # Files representations keep the database directory even on Fabric.
    assert doc["folders"] == ["Files/T0/Raw/Drop"]
