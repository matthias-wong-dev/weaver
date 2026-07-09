from __future__ import annotations

from pathlib import Path

from dbrep_helpers import make_config, resolve, write_python_folder, write_python_table
from weaver_runtime.dbrep.build import BuildPair, BuildRequest, plan_build
from weaver_runtime.dbrep.build.manifest import (
    build_load_plan,
    build_manifest,
    build_source_hashes,
)


def _plan(tmp_path: Path):
    ses_root = tmp_path / "SES"
    servers = {
        "SES_Repo": {"server": str(ses_root)},
        "Lake": {"server": str(tmp_path / "lake")},
    }
    databases = {
        "T0_SES": {"type": "SES", "server": "SES_Repo", "database": "T0"},
        "T1_SES": {"type": "SES", "server": "SES_Repo", "database": "T1"},
        "T0_FILES": {"type": "Files", "server": "Lake", "database": "T0"},
        "T1_DELTA": {"type": "Delta", "server": "Lake", "database": "T1"},
    }
    config = make_config(tmp_path, servers, databases)
    write_python_folder(ses_root / "T0", "Raw", "Drop")
    write_python_table(ses_root / "T1", "Stage", "Record", deps=("T0.Raw.Drop",))
    write_python_table(ses_root / "T1", "Mart", "RecordCurrent", deps=("Stage.Record",))
    write_python_table(ses_root / "T1", "Mart", "Reference", primary_key="id", static=True)
    return plan_build(
        BuildRequest(
            pairs=(
                BuildPair(resolve(config, "T0_SES"), resolve(config, "T0_FILES")),
                BuildPair(resolve(config, "T1_SES"), resolve(config, "T1_DELTA")),
            )
        )
    )


def test_manifest_records_objects_and_dependency_scopes(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    manifest = build_manifest(
        plan.objects,
        target_server="Lake",
        installed_from=[o.source_alias for o in plan.objects],
        installed_to=[o.target_alias for o in plan.objects],
        external_dependencies=plan.external_dependencies,
        installed_at="2026-07-09T00:00:00Z",
        version="9.9.9",
    )

    assert manifest["target_server"] == "Lake"
    assert manifest["runtime_root"] == "Files/_weaver/runtime"
    assert manifest["installed_at"] == "2026-07-09T00:00:00Z"
    assert manifest["weaver_version"] == "9.9.9"

    by_id = {entry["id"]: entry for entry in manifest["objects"]}
    assert by_id["T0.Raw.Drop"]["kind"] == "Folder"
    assert by_id["T0.Raw.Drop"]["materialisation"] == "Files/T0/Raw/Drop"
    assert by_id["T1.Stage.Record"]["dependencies"] == [
        {"id": "T0.Raw.Drop", "scope": "managed_cross_database"}
    ]
    assert by_id["T1.Mart.RecordCurrent"]["dependencies"] == [
        {"id": "T1.Stage.Record", "scope": "intra_database"}
    ]
    assert by_id["T1.Mart.Reference"]["static"] is True


def test_load_plan_is_topologically_sorted_and_records_static(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    load_plan = build_load_plan(plan.objects, server="Lake", targets=["T0_FILES", "T1_DELTA"])

    order = [step["object"] for step in load_plan["steps"]]
    assert order.index("T0.Raw.Drop") < order.index("T1.Stage.Record")
    assert order.index("T1.Stage.Record") < order.index("T1.Mart.RecordCurrent")

    actions = {step["object"]: step["action"] for step in load_plan["steps"]}
    assert actions["T0.Raw.Drop"] == "run_load"
    assert actions["T1.Stage.Record"] == "run_read_and_apply_policy"

    # Static filtering is a load-time decision so --include-static can work.
    assert "T1.Mart.Reference" in order


def test_source_hashes_cover_every_object(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    hashes = build_source_hashes(plan.objects)
    assert set(hashes) == {o.id for o in plan.objects}
    assert all(len(digest) == 64 for digest in hashes.values())
