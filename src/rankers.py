"""Ranker interfaces and the frozen v1 implementations.

The v2 search pipeline only needs :func:`rrf_merge`; the classes below provide
the v3-shaped interface for the later ranker milestones.  A ranker is pure:
it must not read caches, call the network, write files, or use wall-clock time.
"""

from __future__ import annotations

from typing import Protocol

from .records import identity

RRF_K = 60
RRF_ORIGINAL_WEIGHT = 1.0
RRF_EXPANSION_WEIGHT = 0.5

RANKER_VERSIONS = {
    "baseline": "baseline-v1",
    "rrf": "rrf-v1",
    "graph_rerank": "graph-rerank-v1",
}

ZERO_COMPONENTS = {
    "acemap": 0.0,
    "graph": 0.0,
    "text": 0.0,
    "freshness": 0.0,
    "personalization": 0.0,
}


class Ranker(Protocol):
    """Pure ranking component; see docs/interface_protocol.md §5."""

    name: str
    version: str

    def rank(self, query, term_results, graph_context=None):
        """Return ``{"results": [...], "metrics": {...}}``."""


def _failed_count(term_results):
    return sum(item.get("status") != "ok" for item in term_results)


def rrf_merge(query, term_results, k=RRF_K,
              original_weight=RRF_ORIGINAL_WEIGHT,
              expansion_weight=RRF_EXPANSION_WEIGHT):
    """Merge ordered term results with the exact v2 weighted-RRF rules.

    Returns ``(ranked_items, metrics)``.  ``ranked_items`` preserve the v2
    object shape and insertion order: key, paper, fusion_score, matches.
    """
    merged = {}
    baseline_ids = set()
    for term_index, item in enumerate(term_results):
        if item.get("status") != "ok":
            continue
        is_original = item.get("is_original", term_index == 0)
        weight = original_weight if is_original else expansion_weight
        seen_in_term = set()
        for rank, record in enumerate(item.get("records", []), 1):
            key = identity(record)
            if key in seen_in_term:
                continue
            seen_in_term.add(key)
            if is_original:
                baseline_ids.add(key)
            if key not in merged:
                merged[key] = {"key": key, "paper": record,
                               "fusion_score": 0.0, "matches": []}
            entry = merged[key]
            entry["fusion_score"] += weight / (k + rank)
            entry["matches"].append({"term": item["term"], "rank": rank,
                                     "is_original": is_original})
    ranked = sorted(merged.values(), key=lambda entry: (-entry["fusion_score"], entry["key"]))
    metrics = {
        "baseline_unique": len(baseline_ids),
        "enhanced_unique": len(merged),
        "additional_unique": len(set(merged) - baseline_ids),
        "failed_requests": _failed_count(term_results),
    }
    return ranked, metrics


def _v3_result(entry, acemap, components=None):
    """Build one v3 result object in the frozen field order."""
    if components is None:
        components = dict(ZERO_COMPONENTS)
    components = dict(components)
    components["acemap"] = acemap
    return {
        "key": entry["key"],
        "paper": entry["paper"],
        "score": sum(components.values()),
        "score_components": {
            "acemap": components["acemap"],
            "graph": components["graph"],
            "text": components["text"],
            "freshness": components["freshness"],
            "personalization": components["personalization"],
        },
        "evidence": {
            "matched_terms": [match["term"] for match in entry["matches"]],
            "graph_paths": [],
            "matched_authors": [],
            "matched_institutions": [],
        },
        "matches": entry["matches"],
    }


class RRFRanker:
    """The v2 RRF formula expressed through the v3 ranker interface."""

    name = "rrf"
    version = RANKER_VERSIONS["rrf"]

    def __init__(self, k=RRF_K, original_weight=RRF_ORIGINAL_WEIGHT,
                 expansion_weight=RRF_EXPANSION_WEIGHT):
        self.k = k
        self.original_weight = original_weight
        self.expansion_weight = expansion_weight

    def rank(self, query, term_results, graph_context=None):
        ranked, metrics = rrf_merge(
            query, term_results, self.k, self.original_weight, self.expansion_weight)
        maximum = max((entry["fusion_score"] for entry in ranked), default=0.0)
        results = []
        for entry in ranked:
            acemap = entry["fusion_score"] / maximum if maximum > 0 else 0.0
            results.append(_v3_result(entry, acemap))
        return {"results": results, "metrics": metrics}


class BaselineRanker:
    """Keep the original Acemap order and score by normalized reciprocal rank."""

    name = "baseline"
    version = RANKER_VERSIONS["baseline"]

    def rank(self, query, term_results, graph_context=None):
        original = next(
            (item for item in term_results if item.get("is_original")),
            term_results[0] if term_results else None)
        metrics = {
            "baseline_unique": 0,
            "enhanced_unique": 0,
            "additional_unique": 0,
            "failed_requests": _failed_count(term_results),
        }
        if original is None or original.get("status") != "ok":
            return {"results": [], "metrics": metrics}

        ranked = []
        seen = set()
        for rank, record in enumerate(original.get("records", []), 1):
            key = identity(record)
            if key in seen:
                continue
            seen.add(key)
            ranked.append({
                "key": key,
                "paper": record,
                "fusion_score": 0.0,
                "matches": [{"term": original["term"], "rank": rank,
                             "is_original": True}],
            })
        maximum = max((1.0 / (RRF_K + entry["matches"][0]["rank"]) for entry in ranked),
                      default=0.0)
        results = []
        for entry in ranked:
            raw = 1.0 / (RRF_K + entry["matches"][0]["rank"])
            acemap = raw / maximum if maximum > 0 else 0.0
            results.append(_v3_result(entry, acemap))
        metrics.update({
            "baseline_unique": len(ranked),
            "enhanced_unique": len(ranked),
            "additional_unique": 0,
        })
        return {"results": results, "metrics": metrics}


RANKERS = {
    RRFRanker.name: RRFRanker,
    BaselineRanker.name: BaselineRanker,
}


def get_ranker(name="rrf", **kwargs):
    """Return a registered ranker by name; unknown names fail explicitly."""
    try:
        return RANKERS[name](**kwargs)
    except KeyError as exc:
        raise ValueError(f"Unknown ranker: {name}") from exc
