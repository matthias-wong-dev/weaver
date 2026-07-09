from __future__ import annotations

import textwrap

import pytest

from weaver_runtime.dbrep.errors import DependencyError
from weaver_runtime.dbrep.ses.python_discovery import extract_python_references


def test_finds_two_and_three_part_self_repo_references() -> None:
    source = textwrap.dedent(
        '''
        """
        Table ID: Stage.Record
        Description: x
        Lineage: y
        """

        class StageRecord:
            def read(self, spark):
                drop = self.repo["T0.Raw.Drop"]
                prior = self.repo["Stage.Prior"]
                return drop.join(prior)
        '''
    )
    refs = extract_python_references(source)
    assert ("T0", "Raw", "Drop") in refs
    assert ("Stage", "Prior") in refs


def test_deduplicates_and_preserves_order() -> None:
    source = textwrap.dedent(
        '''
        """doc"""
        class X:
            def read(self, spark):
                a = self.repo["A.B"]
                b = self.repo["C.D"]
                a2 = self.repo["A.B"]
        '''
    )
    assert extract_python_references(source) == (("A", "B"), ("C", "D"))


def test_ignores_non_self_repo_subscripts_and_dynamic_keys() -> None:
    source = textwrap.dedent(
        '''
        """doc"""
        class X:
            def read(self, spark):
                other = some_dict["A.B"]
                name = "C.D"
                dyn = self.repo[name]
                real = self.repo["E.F"]
        '''
    )
    assert extract_python_references(source) == (("E", "F"),)


def test_four_part_reference_is_returned() -> None:
    source = '"""doc"""\nclass X:\n    def read(self, spark):\n        x = self.repo["Srv.Db.Schema.Object"]\n'
    assert extract_python_references(source) == (("Srv", "Db", "Schema", "Object"),)


def test_single_part_reference_is_rejected() -> None:
    source = '"""doc"""\nclass X:\n    def read(self, spark):\n        x = self.repo["Record"]\n'
    with pytest.raises(DependencyError, match="at least Schema.Object"):
        extract_python_references(source)
