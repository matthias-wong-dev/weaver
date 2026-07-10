from __future__ import annotations

import json
from pathlib import Path

import pytest

from dbrep_helpers import load_config, resolve, write_config_files, write_python_folder, write_python_table
from weaver_runtime.dbrep.build import BuildPair, BuildRequest, plan_build
from weaver_runtime.dbrep.errors import BuildError, ProgramError
from weaver_runtime.dbrep.execution import execute_program_local
from weaver_runtime.dbrep.lakehouse.artifacts import (
    generate_lakehouse_artifacts,
    group_lakehouse_objects_by_host,
    render_host_program,
)
from weaver_runtime.dbrep.lakehouse.programs import render_build_program, render_load_program
from weaver_runtime.fabric import livy

SCHEMA = (("record_id", "string"), ("group_id", "string"), ("amount", "int"))


def _plan(tmp_path: Path, *, with_schema: bool = True):
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
    weaver_path = write_config_files(tmp_path, servers, databases)
    write_python_folder(ses_root / "T0", "Raw", "Drop")
    schema_cols = SCHEMA if with_schema else ()
    write_python_table(ses_root / "T1", "Stage", "Record", deps=("T0.Raw.Drop",), schema_cols=schema_cols)
    write_python_table(ses_root / "T1", "Mart", "Record", deps=("Stage.Record",), schema_cols=schema_cols)
    config = load_config(weaver_path)
    plan = plan_build(
        BuildRequest(
            pairs=(
                BuildPair(resolve(config, "T0_SES"), resolve(config, "T0_FILES")),
                BuildPair(resolve(config, "T1_SES"), resolve(config, "T1_DELTA")),
            )
        )
    )
    return weaver_path, plan


def _host_objects(plan):
    return group_lakehouse_objects_by_host(plan, fabric=False)[0].objects


# --- 8.1 Program rendering --------------------------------------------------


def test_build_program_only_includes_delta_tables_in_plan_order(tmp_path: Path) -> None:
    _, plan = _plan(tmp_path)
    program = render_build_program(_host_objects(plan))
    specs = _extract_specs(program)

    ids = [spec["id"] for spec in specs]
    assert ids == ["T1.Stage.Record", "T1.Mart.Record"]  # Folder excluded, plan order kept
    assert "T0.Raw.Drop" not in program
    assert specs[0]["schema"] == [["record_id", "string"], ["group_id", "string"], ["amount", "int"]]


def test_build_program_is_deterministic_and_environment_free(tmp_path: Path) -> None:
    _, plan = _plan(tmp_path)
    objects = _host_objects(plan)
    first = render_build_program(objects)
    second = render_build_program(objects)
    assert first == second

    assert "WEAVER_RESULT" in first
    assert str(tmp_path) not in first  # no local absolute roots
    for forbidden in ("abfss", "onelake", "notebookutils", "workspace_id", "lakehouse_id"):
        assert forbidden not in first


def test_build_program_missing_schema_fails_rendering(tmp_path: Path) -> None:
    _, plan = _plan(tmp_path, with_schema=False)
    with pytest.raises(BuildError) as excinfo:
        render_build_program(_host_objects(plan))
    assert "T1.Stage.Record" in str(excinfo.value)
    assert "requires a declared schema" in str(excinfo.value)


def test_load_program_embeds_filters_and_is_deterministic(tmp_path: Path) -> None:
    program = render_load_program(
        target_filter="T1_DELTA", object_filter=("T1.Stage.Record",), include_static=True, strict=False
    )
    assert program == render_load_program(
        target_filter="T1_DELTA", object_filter=("T1.Stage.Record",), include_static=True, strict=False
    )
    assert "load_target_runtime" in program
    assert "'T1_DELTA'" in program
    assert "('T1.Stage.Record',)" in program
    assert "include_static=True" in program
    assert "strict=False" in program
    assert "WEAVER_RESULT" in program


# --- 8.2 Generic local executor ---------------------------------------------


def test_execute_program_local_provides_globals_and_returns_result(tmp_path: Path) -> None:
    program = (
        "WEAVER_RESULT = {\n"
        "    'runtime': WEAVER_RUNTIME_ROOT,\n"
        "    'spark_root': WEAVER_SPARK_ROOT,\n"
        "    'has_spark': spark is not None,\n"
        "}\n"
    )
    result = execute_program_local(
        program, spark=None, runtime_root=tmp_path / "rt", spark_root=tmp_path / "lake"
    )
    assert result == {
        "runtime": str(tmp_path / "rt"),
        "spark_root": str(tmp_path / "lake"),
        "has_spark": False,
    }


def test_execute_program_local_imports_from_bundled_runtime_and_restores_path(tmp_path: Path) -> None:
    import sys

    runtime_root = tmp_path / "rt"
    orchestrator = runtime_root / "_orchestrator"
    orchestrator.mkdir(parents=True)
    (orchestrator / "weaver_probe.py").write_text("VALUE = 42\n", encoding="utf-8")

    program = "import weaver_probe\nWEAVER_RESULT = {'value': weaver_probe.VALUE}\n"
    result = execute_program_local(program, spark=None, runtime_root=runtime_root, spark_root=tmp_path)
    assert result == {"value": 42}
    assert str(orchestrator) not in sys.path  # restored


def test_execute_program_local_requires_json_result(tmp_path: Path) -> None:
    with pytest.raises(ProgramError, match="did not set WEAVER_RESULT"):
        execute_program_local("x = 1\n", spark=None, runtime_root=tmp_path, spark_root=tmp_path)

    with pytest.raises(ProgramError, match="not JSON-serialisable"):
        execute_program_local(
            "WEAVER_RESULT = object()\n", spark=None, runtime_root=tmp_path, spark_root=tmp_path
        )


def test_execute_program_local_restores_path_on_failure(tmp_path: Path) -> None:
    import sys

    orchestrator = str(tmp_path / "_orchestrator")
    with pytest.raises(ValueError):
        execute_program_local(
            "raise ValueError('boom')\n", spark=None, runtime_root=tmp_path, spark_root=tmp_path
        )
    assert orchestrator not in sys.path


# --- 8.3 Generic Livy runtime submitter -------------------------------------


def test_run_runtime_program_is_operation_agnostic_and_runs_exact_program(monkeypatch) -> None:
    captured: dict = {}

    def fake_run_code(workspace_id, lakehouse_id, token, code, **kwargs):
        captured["code"] = code
        return {"data": {"text/plain": livy.RUNTIME_RESULT_MARKER + json.dumps({"ok": True})}}

    monkeypatch.setattr(livy, "run_code", fake_run_code)

    program = "WEAVER_RESULT = {'hello': 'world'}\n"
    result = livy.run_runtime_program("ws-1", "lh-1", "token", program, api_version="2023-01-01")

    assert result == {"ok": True}
    code = captured["code"]
    # The exact program is embedded verbatim and executed.
    assert repr(program) in code
    assert "ws-1" in code and "lh-1" in code
    # Generic bootstrap knows nothing about the operation.
    for forbidden in ("initialise_delta_tables", "load_target_runtime", "_SPECS", "target_filter"):
        assert forbidden not in code


def test_parse_runtime_result_requires_marker() -> None:
    good = f"noise\n{livy.RUNTIME_RESULT_MARKER}{json.dumps({'a': 1})}\nmore\n"
    assert livy.parse_runtime_result(good) == {"a": 1}
    with pytest.raises(livy.LivyError, match="did not return"):
        livy.parse_runtime_result("no marker here")


# --- 8.4 Exact-code parity ---------------------------------------------------


def test_generated_build_program_is_the_exact_local_and_fabric_string(tmp_path: Path) -> None:
    _, plan = _plan(tmp_path)
    group = group_lakehouse_objects_by_host(plan, fabric=False)[0]
    out_dir = tmp_path / "gen"

    [artifact] = generate_lakehouse_artifacts(plan, out_dir)
    local_program = render_host_program(group)  # what local build executes

    # Fabric build applies the artifact's program; both derive from one renderer.
    assert artifact.program == local_program
    # build.py on disk is byte-for-byte the executed string.
    assert artifact.build_program_path.read_text(encoding="utf-8") == local_program


# --- 8.6 / 8.7 Generation artifacts -----------------------------------------


def test_generate_emits_files_build_py_and_plan_per_host(tmp_path: Path) -> None:
    _, plan = _plan(tmp_path)
    out_dir = tmp_path / "gen"
    [artifact] = generate_lakehouse_artifacts(plan, out_dir)

    host = out_dir / "Lake"
    assert artifact.root == host
    assert (host / "build.py").is_file()
    assert (host / "build-plan.json").is_file()
    # Files/ produced by install_build: managed folder marker + runtime bundle.
    assert (host / "Files" / "T0" / "Raw" / "Drop" / "_weaver.json").is_file()
    assert (host / "Files" / "_weaver" / "runtime" / "manifest.json").is_file()

    doc = json.loads((host / "build-plan.json").read_text(encoding="utf-8"))
    assert doc["server"] == "Lake"
    assert doc["objects"] == ["T0.Raw.Drop", "T1.Stage.Record", "T1.Mart.Record"]
    assert doc["folders"] == ["Files/T0/Raw/Drop"]
    assert doc["delta_tables"] == ["Tables/T1/Stage.Record", "Tables/T1/Mart.Record"]


def test_generate_fails_before_writes_when_schema_missing(tmp_path: Path) -> None:
    _, plan = _plan(tmp_path, with_schema=False)
    out_dir = tmp_path / "gen"
    with pytest.raises(BuildError, match="requires a declared schema"):
        generate_lakehouse_artifacts(plan, out_dir)
    assert not (out_dir / "Lake").exists()  # no partial artifact


def test_staged_runtime_source_matches_live_package(tmp_path: Path) -> None:
    # Parity is at the source level: the staged _orchestrator bundle the program
    # imports on Fabric is a byte-for-byte copy of the live runtime the local
    # executor imports. (The exact program string is asserted elsewhere.)
    import hashlib

    import weaver_runtime.dbrep.runtime.initialise as live_initialise
    import weaver_runtime.dbrep.runtime.load as live_load
    import weaver_runtime.dbrep.runtime.orchestrator as live_orchestrator

    _, plan = _plan(tmp_path)
    [artifact] = generate_lakehouse_artifacts(plan, tmp_path / "gen")
    staged = (
        artifact.files_root / "_weaver" / "runtime" / "_orchestrator"
        / "weaver_runtime" / "dbrep" / "runtime"
    )

    def sha(path: Path) -> str:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()

    for module in (live_initialise, live_load, live_orchestrator):
        staged_file = staged / Path(module.__file__).name
        assert staged_file.is_file()
        assert sha(staged_file) == sha(module.__file__)


def _extract_specs(program: str) -> list[dict]:
    # The program embeds specs as ``json.loads(<repr of a json string>)``.
    import ast

    marker = "json.loads("
    start = program.index(marker) + len(marker)
    end = program.index(")", start)
    return json.loads(ast.literal_eval(program[start:end].strip()))
