"""Per-database helper isolation for object import.

Objects import their helpers relative to their own database folder
(``from ._helpers…``). Two databases may ship like-named ``_helpers`` with
different content; importing objects from both in one process (a no-``--target``
load) must never alias one database's helpers over the other's.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from weaver_runtime.dbrep.runtime.load import _import_object_module


def _make_database(root: Path, database: str, marker: str) -> Path:
    """Write a database folder with a folder-local helper and one object."""

    db_dir = root / database
    source = db_dir / "_helpers" / "source"
    source.mkdir(parents=True)
    (db_dir / "_helpers" / "__init__.py").write_text("", encoding="utf-8")
    (source / "__init__.py").write_text("", encoding="utf-8")
    # Same module name in both databases, different content.
    (source / "shared.py").write_text(f"WHO = {marker!r}\n", encoding="utf-8")
    # A helper importing a sibling relatively (helper<->helper).
    (source / "uses_sibling.py").write_text(
        "from .shared import WHO\nORIGIN = WHO\n", encoding="utf-8"
    )
    # The object importing its folder-local helpers relatively.
    object_path = db_dir / f"{database}__Obj.py"
    object_path.write_text(
        "from ._helpers.source import shared\n"
        "from ._helpers.source.uses_sibling import ORIGIN\n"
        "RESULT = (shared.WHO, ORIGIN)\n",
        encoding="utf-8",
    )
    return object_path


def test_like_named_helpers_do_not_alias_across_databases(tmp_path: Path) -> None:
    t0_object = _make_database(tmp_path, "T0_DWG", "T0-download")
    t1_object = _make_database(tmp_path, "T1_DWG", "T1-transform")

    # Import BOTH objects in one process (the no-``--target`` collision scenario).
    t0_module = _import_object_module(
        SimpleNamespace(source_path=t0_object, database="T0_DWG")
    )
    t1_module = _import_object_module(
        SimpleNamespace(source_path=t1_object, database="T1_DWG")
    )

    # Each object resolves its OWN folder's helpers, at both the object and the
    # helper<->helper level.
    assert t0_module.RESULT == ("T0-download", "T0-download")
    assert t1_module.RESULT == ("T1-transform", "T1-transform")
