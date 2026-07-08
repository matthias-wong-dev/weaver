from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import ModuleType


def scripts_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "scripts"


def load_script_module(name: str) -> ModuleType:
    directory = str(scripts_dir())
    if directory not in sys.path:
        sys.path.insert(0, directory)
    return importlib.import_module(name)


def run_legacy_main(module_name: str, argv: list[str]) -> int:
    module = load_script_module(module_name)
    previous_argv = sys.argv[:]
    sys.argv = [module_name, *argv]
    try:
        return int(module.main())
    finally:
        sys.argv = previous_argv
