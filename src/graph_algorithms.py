"""Deterministic, offline graph algorithms used by the later milestones.

The functions in this module operate on the edge dictionaries emitted by the
ConceptNet index/provider.  They deliberately return bounded scores and keep
the selected path and algorithm parameters beside every candidate so that a
result can be inspected and replayed without reading global state.
"""

from __future__ import annotations

import heapq
import math
from collections import defaultdict
from typing import Iterable

from .text import normalize


ALGORITHMS = ("pagerank", "weighted_path", "common_neighbors", "adamic_adar",
              "community", "meta_path")


def _clean_edges(edges: Iterable[dict]) -> list[dict]:
    cleaned = []
    for edge in edges or []:
        if not isinstance(edge, dict):
            continue
        subject = normalize(edge.get("subject", ""))
        obj = normalize(edge.get("object", ""))
        relation = str(edge.get("relation", ""))
        assertion = str(edge.get("assertion", ""))
        try:
            weight = float(edge.get("weight", 0.0))
        except (TypeError, ValueError):
            continue
        if subject and obj and subject != obj and math.isfinite(weight) and weight > 0:
            cleaned.append({"subject": subject, "object": obj, "relation": relation,
                            "assertion": assertion, "weight": weight})
    return sorted(cleaned, key=lambda e: (e["subject"], e["object"],
                                          e["relation"], e["assertion"]))


def _adjacency(edges: Iterable[dict]):
    adjacency = defaultdict(dict)
    for edge in _clean_edges(edges):
        # ConceptNet relations are treated as undirected for discovery.  The
        # original assertion is retained for the explanation path.
        for left, right in ((edge["subject"], edge["object"]),
                            (edge["object"], edge["subject"])):
            current = adjacency[left].get(right)
            if current is None or edge["weight"] > current["weight"]:
                adjacency[left][right] = edge
    return adjacency


def _normalise_scores(scores: dict[str, float]) -> dict[str, float]:
    maximum = max(scores.values(), default=0.0)
    if maximum <= 0:
        return {key: 0.0 for key in scores}
    return {key: min(1.0, max(0.0, value / maximum)) for key, value in scores.items()}


def pagerank(edges: Iterable[dict], seeds: Iterable[str], damping: float = 0.85,
             iterations: int = 50, tolerance: float = 1e-10) -> dict[str, float]:
    """Return deterministic personalised PageRank scores for graph nodes."""
    if not 0 <= damping < 1 or isinstance(iterations, bool) or iterations < 1:
        raise ValueError("invalid PageRank parameters")
    if tolerance <= 0 or not math.isfinite(tolerance):
        raise ValueError("tolerance must be positive and finite")
    adjacency = _adjacency(edges)
    nodes = sorted(set(adjacency) | {node for values in adjacency.values() for node in values})
    seeds = sorted({normalize(seed) for seed in seeds if normalize(seed) in nodes})
    if not nodes:
        return {}
    teleport = {node: (1.0 / len(seeds) if node in seeds else 0.0) for node in nodes}
    if not seeds:
        teleport = {node: 1.0 / len(nodes) for node in nodes}
    scores = dict(teleport)
    for _ in range(iterations):
        updated = {node: (1.0 - damping) * teleport[node] for node in nodes}
        for node in nodes:
            neighbours = adjacency.get(node, {})
            if not neighbours:
                share = damping * scores[node] / len(nodes)
                for target in nodes:
                    updated[target] += share
                continue
            total = sum(max(edge["weight"], 0.0) for edge in neighbours.values())
            if total <= 0:
                continue
            for target, edge in neighbours.items():
                updated[target] += damping * scores[node] * edge["weight"] / total
        delta = sum(abs(updated[node] - scores[node]) for node in nodes)
        scores = updated
        if delta <= tolerance:
            break
    return _normalise_scores(scores)


def weighted_shortest_paths(edges: Iterable[dict], seeds: Iterable[str],
                            max_hops: int = 3) -> dict[str, float]:
    """Score nodes by the best high-weight path from any seed.

    Edge cost is ``1 / weight``.  The returned score is ``exp(-cost)`` and is
    therefore always in ``(0, 1]`` for reachable nodes.
    """
    if isinstance(max_hops, bool) or max_hops < 1:
        raise ValueError("max_hops must be >= 1")
    adjacency = _adjacency(edges)
    starts = sorted({normalize(seed) for seed in seeds if normalize(seed) in adjacency})
    best: dict[tuple[str, int], float] = {(seed, 0): 0.0 for seed in starts}
    queue = [(0.0, seed, 0) for seed in starts]
    heapq.heapify(queue)
    while queue:
        cost, node, hops = heapq.heappop(queue)
        if cost > best.get((node, hops), float("inf")) + 1e-15 or hops >= max_hops:
            continue
        for target, edge in sorted(adjacency.get(node, {}).items()):
            next_cost = cost + 1.0 / max(edge["weight"], 1e-12)
            state = (target, hops + 1)
            if next_cost < best.get(state, float("inf")):
                best[state] = next_cost
                heapq.heappush(queue, (next_cost, target, hops + 1))
    scores: dict[str, float] = {}
    for (node, _), cost in best.items():
        scores[node] = max(scores.get(node, 0.0), math.exp(-cost))
    return _normalise_scores(scores)


def common_neighbors(edges: Iterable[dict], seeds: Iterable[str]) -> dict[str, float]:
    adjacency = _adjacency(edges)
    seed_set = {normalize(seed) for seed in seeds}
    candidates = set().union(*(set(adjacency.get(seed, {})) for seed in seed_set)) if seed_set else set()
    scores = {}
    for candidate in candidates:
        if candidate in seed_set:
            continue
        scores[candidate] = float(sum(
            len(set(adjacency.get(seed, {})) & set(adjacency.get(candidate, {})))
            for seed in seed_set))
    return _normalise_scores(scores)


def adamic_adar(edges: Iterable[dict], seeds: Iterable[str]) -> dict[str, float]:
    adjacency = _adjacency(edges)
    seed_set = {normalize(seed) for seed in seeds}
    degrees = {node: len(neighbours) for node, neighbours in adjacency.items()}
    candidates = set().union(*(set(adjacency.get(seed, {})) for seed in seed_set)) if seed_set else set()
    scores = {}
    for candidate in candidates:
        if candidate in seed_set:
            continue
        value = 0.0
        for seed in seed_set:
            for neighbour in set(adjacency.get(seed, {})) & set(adjacency.get(candidate, {})):
                degree = degrees.get(neighbour, 0)
                if degree > 1:
                    value += 1.0 / math.log(degree)
        scores[candidate] = value
    return _normalise_scores(scores)


def community_scores(edges: Iterable[dict], seeds: Iterable[str]) -> dict[str, float]:
    """Score nodes by neighbourhood overlap with the seed neighbourhood.

    This deterministic local community coefficient is useful for small query
    subgraphs and avoids depending on a non-standard Louvain package.  It is
    equivalent to a weighted Jaccard membership signal and remains bounded.
    """
    adjacency = _adjacency(edges)
    seed_set = {normalize(seed) for seed in seeds}
    seed_neighbours = set().union(*(set(adjacency.get(seed, {})) for seed in seed_set)) \
        if seed_set else set()
    scores = {}
    for candidate in set().union(*(set(adjacency.get(seed, {})) for seed in seed_set)) \
            if seed_set else set():
        if candidate in seed_set:
            continue
        neighbours = set(adjacency.get(candidate, {}))
        union = seed_neighbours | neighbours
        if union:
            scores[candidate] = len(seed_neighbours & neighbours) / len(union)
    return _normalise_scores(scores)


def meta_path_scores(edges: Iterable[dict], seeds: Iterable[str],
                     relation_sequence: Iterable[str]) -> dict[str, float]:
    """Follow a fixed relation sequence and return product-weight scores."""
    sequence = [str(relation) for relation in relation_sequence]
    if not sequence:
        raise ValueError("relation_sequence must not be empty")
    edge_list = _clean_edges(edges)
    adjacency = defaultdict(list)
    for edge in edge_list:
        adjacency[edge["subject"]].append(edge)
        # Discovery is undirected, while a meta-path still constrains labels.
        adjacency[edge["object"]].append({**edge, "subject": edge["object"],
                                           "object": edge["subject"]})
    frontier = {normalize(seed): 1.0 for seed in seeds if normalize(seed)}
    for relation in sequence:
        next_frontier = {}
        for node, score in sorted(frontier.items()):
            for edge in sorted(adjacency.get(node, []),
                               key=lambda item: (item["object"], item["assertion"])):
                if edge["relation"] != relation:
                    continue
                candidate = edge["object"]
                next_frontier[candidate] = max(
                    next_frontier.get(candidate, 0.0),
                    score * min(1.0, edge["weight"]))
        frontier = next_frontier
        if not frontier:
            break
    return _normalise_scores(frontier)


def rank_terms(edges: Iterable[dict], seeds: Iterable[str], algorithm: str = "pagerank",
               top_k: int = 10, **parameters) -> list[dict]:
    """Rank graph terms and attach a deterministic one or multi-hop path."""
    if algorithm not in ALGORITHMS:
        raise ValueError(f"unknown graph algorithm: {algorithm}")
    if isinstance(top_k, bool) or top_k < 1:
        raise ValueError("top_k must be >= 1")
    edge_list = _clean_edges(edges)
    seed_list = sorted({normalize(seed) for seed in seeds if normalize(seed)})
    effective = dict(parameters)
    if algorithm == "pagerank":
        effective.setdefault("damping", 0.85)
        effective.setdefault("iterations", 50)
        effective.setdefault("tolerance", 1e-10)
    elif algorithm == "weighted_path":
        effective.setdefault("max_hops", 3)
    if algorithm == "pagerank":
        scores = pagerank(edge_list, seed_list, damping=effective["damping"],
                          iterations=effective["iterations"],
                          tolerance=effective["tolerance"])
    elif algorithm == "weighted_path":
        scores = weighted_shortest_paths(edge_list, seed_list,
                                         max_hops=effective["max_hops"])
    elif algorithm == "common_neighbors":
        scores = common_neighbors(edge_list, seed_list)
    elif algorithm == "adamic_adar":
        scores = adamic_adar(edge_list, seed_list)
    elif algorithm == "community":
        scores = community_scores(edge_list, seed_list)
    else:
        scores = meta_path_scores(edge_list, seed_list,
                                  effective.get("relation_sequence", []))
    adjacency = _adjacency(edge_list)

    def explain_path(target):
        # Deterministic breadth-first explanation path for candidates that are
        # more than one edge away from a seed.
        queue = [(seed, [seed]) for seed in seed_list]
        seen = set(seed_list)
        max_hops = int(effective.get("max_hops", 3))
        while queue:
            node, nodes = queue.pop(0)
            if node == target:
                path = []
                for left, right in zip(nodes, nodes[1:]):
                    edge = adjacency[left].get(right)
                    if edge is not None:
                        path.append({"nodes": [left, right], "relation": edge["relation"],
                                     "assertion": edge["assertion"], "weight": edge["weight"]})
                return path
            if len(nodes) - 1 >= max_hops:
                continue
            for neighbour in sorted(adjacency.get(node, {})):
                if neighbour not in seen:
                    seen.add(neighbour)
                    queue.append((neighbour, nodes + [neighbour]))
        return []

    result = []
    for term, score in sorted(scores.items(), key=lambda item: (-item[1], item[0])):
        if term in seed_list or score <= 0:
            continue
        path = explain_path(term)
        result.append({"term": term, "score": float(score), "path": path,
                       "algorithm": algorithm,
                       "parameters": {key: effective[key] for key in sorted(effective)}})
        if len(result) >= top_k:
            break
    return result


def analyze_graph(edges: Iterable[dict], seeds: Iterable[str], algorithm: str = "pagerank",
                  top_k: int = 10, source: dict | None = None, **parameters) -> dict:
    """Produce a stable standalone graph-analysis document."""
    edge_list = _clean_edges(edges)
    seed_list = sorted({normalize(seed) for seed in seeds if normalize(seed)})
    candidates = rank_terms(edge_list, seed_list, algorithm, top_k, **parameters)
    effective_parameters = dict(parameters)
    if algorithm == "pagerank":
        effective_parameters.setdefault("damping", 0.85)
        effective_parameters.setdefault("iterations", 50)
        effective_parameters.setdefault("tolerance", 1e-10)
    elif algorithm == "weighted_path":
        effective_parameters.setdefault("max_hops", 3)
    nodes = set()
    for edge in edge_list:
        nodes.update((edge["subject"], edge["object"]))
    if candidates:
        effective_parameters = candidates[0]["parameters"]
    return {
        "schema_version": "graph-analysis-v1",
        "source": source,
        "algorithm": algorithm,
        "parameters": effective_parameters,
        "seeds": seed_list,
        "summary": {"edges": len(edge_list), "nodes": len(nodes),
                    "candidates": len(candidates)},
        "candidates": candidates,
    }
