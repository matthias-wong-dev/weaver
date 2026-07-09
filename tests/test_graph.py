from __future__ import annotations

import pytest

from weaver_runtime.dbrep.errors import GraphError
from weaver_runtime.dbrep.ses.graph import topological_layers, topological_order


def test_orders_dependencies_before_dependents() -> None:
    order = topological_order(
        ["T1.Stage.Record", "T0.Raw.Drop"],
        [("T0.Raw.Drop", "T1.Stage.Record")],
    )
    assert order.index("T0.Raw.Drop") < order.index("T1.Stage.Record")


def test_deterministic_lexicographic_among_independent_nodes() -> None:
    order = topological_order(["b", "a", "c"], [])
    assert order == ["a", "b", "c"]


def test_ignores_edges_to_unknown_nodes() -> None:
    order = topological_order(["a"], [("a", "external"), ("external", "a")])
    assert order == ["a"]


def test_detects_cycle() -> None:
    with pytest.raises(GraphError, match="cycle detected"):
        topological_order(["a", "b"], [("a", "b"), ("b", "a")])


def test_cycle_error_names_the_cycle() -> None:
    with pytest.raises(GraphError, match="a -> b -> c -> a"):
        topological_order(["a", "b", "c"], [("a", "b"), ("b", "c"), ("c", "a")])


def test_topological_layers_groups_independent_nodes() -> None:
    # T0 -> {A, B}; A -> C. Layers: [T0], [A, B], [C]
    layers = topological_layers(
        ["T0", "A", "B", "C"],
        [("T0", "A"), ("T0", "B"), ("A", "C")],
    )
    assert layers == [["T0"], ["A", "B"], ["C"]]


def test_topological_layers_detects_cycle() -> None:
    with pytest.raises(GraphError, match="cycle detected"):
        topological_layers(["a", "b"], [("a", "b"), ("b", "a")])
