"""Generic dependency-graph utilities: topological sort and cycle detection.

Edges are ``(before, after)`` pairs: ``before`` must precede ``after``. Ordering
is deterministic (lexicographic among ready nodes) so build plans are stable.
"""

from __future__ import annotations

import heapq
from collections import defaultdict
from typing import Iterable

from ..errors import GraphError

Edge = "tuple[str, str]"


def topological_order(nodes: Iterable[str], edges: Iterable[tuple[str, str]]) -> list[str]:
    """Return nodes in dependency order, raising :class:`GraphError` on a cycle.

    Edges referencing nodes outside ``nodes`` are ignored (callers validate
    missing dependencies separately).
    """

    node_set = set(nodes)
    adjacency: dict[str, set[str]] = defaultdict(set)
    indegree: dict[str, int] = {node: 0 for node in node_set}

    for before, after in edges:
        if before not in node_set or after not in node_set:
            continue
        if after not in adjacency[before]:
            adjacency[before].add(after)
            indegree[after] += 1

    heap = [node for node in node_set if indegree[node] == 0]
    heapq.heapify(heap)

    order: list[str] = []
    while heap:
        node = heapq.heappop(heap)
        order.append(node)
        for neighbour in sorted(adjacency[node]):
            indegree[neighbour] -= 1
            if indegree[neighbour] == 0:
                heapq.heappush(heap, neighbour)

    if len(order) != len(node_set):
        cycle = _find_cycle(node_set, adjacency)
        detail = " -> ".join(cycle) if cycle else "unknown"
        raise GraphError(f"dependency cycle detected: {detail}")

    return order


def _find_cycle(nodes: set[str], adjacency: dict[str, set[str]]) -> list[str]:
    WHITE, GREY, BLACK = 0, 1, 2
    colour = {node: WHITE for node in nodes}
    stack: list[str] = []

    def visit(node: str) -> list[str] | None:
        colour[node] = GREY
        stack.append(node)
        for neighbour in sorted(adjacency.get(node, ())):
            if colour.get(neighbour) == GREY:
                index = stack.index(neighbour)
                return stack[index:] + [neighbour]
            if colour.get(neighbour) == WHITE:
                found = visit(neighbour)
                if found is not None:
                    return found
        stack.pop()
        colour[node] = BLACK
        return None

    for start in sorted(nodes):
        if colour[start] == WHITE:
            found = visit(start)
            if found is not None:
                return found
    return []
