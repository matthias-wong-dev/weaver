from __future__ import annotations

import os
import sys
from pathlib import Path
from shutil import which

import pytest

_JAVA_CANDIDATES = [
    os.environ.get("JAVA_HOME"),
    "/opt/homebrew/opt/openjdk@17/libexec/openjdk.jdk/Contents/Home",
    "/opt/homebrew/opt/openjdk/libexec/openjdk.jdk/Contents/Home",
    "/usr/local/opt/openjdk@17/libexec/openjdk.jdk/Contents/Home",
]


def _prepare_java_env() -> bool:
    for candidate in _JAVA_CANDIDATES:
        if candidate and Path(candidate, "bin", "java").exists():
            os.environ["JAVA_HOME"] = candidate
            bin_dir = str(Path(candidate, "bin"))
            if bin_dir not in os.environ.get("PATH", ""):
                os.environ["PATH"] = bin_dir + os.pathsep + os.environ.get("PATH", "")
            return True
    return which("java") is not None


@pytest.fixture(scope="session")
def spark():
    if not _prepare_java_env():
        pytest.skip("no Java runtime available for local Spark tests")
    os.environ.setdefault("PYSPARK_PYTHON", sys.executable)
    os.environ.setdefault("PYSPARK_DRIVER_PYTHON", sys.executable)
    try:
        from weaver_runtime.dbrep.runtime.load import create_delta_session

        session = create_delta_session("weaver-tests")
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"local Spark/Delta session unavailable: {exc}")
    session.sparkContext.setLogLevel("ERROR")
    try:
        yield session
    finally:
        session.stop()
