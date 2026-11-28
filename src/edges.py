"""Deterministic ordering of ConceptNet edges and their query-time neighbors."""

from __future__ import annotations

from .text import normalize


def edge_order(edge):
    """Rank by descending weight, then assertion and endpoint URIs."""
    return (-edge["weight"], edge["assertion"], edge["subject_uri"], edge["object_uri"])


def rank_neighbors(edge_list, query, limit):
    """Rank one-hop neighbors of ``query`` in a query-time ConceptNet subgraph.

    Traversal is bidirectional so related terms can be found from either edge
    direction; the stored subject/relation/object orientation is preserved as
    evidence. Neighbors are ordered by their best edge and then by term, which
    keeps selection deterministic across Python runs.

    ``domain_support_seeds`` is part of the established result schema. Dynamic
    graphs have no fixed domain seeds, so it is always empty here.
    """
    if not query or limit < 1:
        return []
    query = normalize(query)
    candidates = {}
    for edge in edge_list:
        if query == edge["subject"]:
            neighbor = edge["object"]
        elif query == edge["object"]:
            neighbor = edge["subject"]
        else:
            continue
        existing = candidates.get(neighbor)
        if existing is None or edge_order(edge) < edge_order(existing):
            candidates[neighbor] = edge
    ordered = sorted(candidates.items(), key=lambda pair: (edge_order(pair[1]), pair[0]))
    return [{"term": term, "evidence": edge, "domain_support_seeds": []}
            for term, edge in ordered[:limit]]
