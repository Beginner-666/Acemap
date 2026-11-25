"""Ranker interfaces and the frozen v1 implementations.

The v2 search pipeline only needs :func:`rrf_merge`; the classes below provide
the v3-shaped interface for the later ranker milestones.  A ranker is pure:
it must not read caches, call the network, write files, or use wall-clock time.
"""

from __future__ import annotations

import math
import re
from typing import Protocol

from . import config as ranking_config
from .paper_features import paper_features
from .records import identity
from .text import normalize

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


def _v3_result(entry, components, weights=None, graph_paths=None):
    """Build one v3 result object in the frozen field order.

    ``weights`` is only supplied by ``graph-rerank-v1``; the baseline/RRF
    rankers keep the historical ``score == acemap`` identity.
    """
    components = {key: float(components.get(key, 0.0)) for key in ZERO_COMPONENTS}
    if weights is None:
        score = sum(components.values())
    else:
        score = (
            weights["acemap"] * components["acemap"]
            + weights["graph"] * components["graph"]
            + weights["text"] * components["text"]
            + weights["freshness"] * components["freshness"]
            + weights["personalization"] * components["personalization"]
        )
    return {
        "key": entry["key"],
        "paper": entry["paper"],
        "score": score,
        "score_components": {
            "acemap": components["acemap"],
            "graph": components["graph"],
            "text": components["text"],
            "freshness": components["freshness"],
            "personalization": components["personalization"],
        },
        "evidence": {
            "matched_terms": [match["term"] for match in entry["matches"]],
            "graph_paths": list(graph_paths or []),
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
                 expansion_weight=RRF_EXPANSION_WEIGHT, config=None):
        if config is not None:
            fusion = config["fusion"]
            k, original_weight, expansion_weight = (
                fusion["k"], fusion["original_weight"], fusion["expansion_weight"])
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
            components = dict(ZERO_COMPONENTS)
            components["acemap"] = acemap
            results.append(_v3_result(entry, components))
        return {"results": results, "metrics": metrics}


class BaselineRanker:
    """Keep the original Acemap order and score by normalized reciprocal rank."""

    name = "baseline"
    version = RANKER_VERSIONS["baseline"]

    def __init__(self, config=None):
        self.config = config

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
            components = dict(ZERO_COMPONENTS)
            components["acemap"] = acemap
            results.append(_v3_result(entry, components))
        metrics.update({
            "baseline_unique": len(ranked),
            "enhanced_unique": len(ranked),
            "additional_unique": 0,
        })
        return {"results": results, "metrics": metrics}


TOKEN_PATTERN = re.compile(r"[a-z0-9]+")


def _tokens(value):
    return [token for token in TOKEN_PATTERN.findall(normalize(value))
            if len(token) > 1]


def _phrase_in(tokens, phrase):
    needle = _tokens(phrase)
    if not needle:
        return False
    width = len(needle)
    return any(tokens[index:index + width] == needle
               for index in range(len(tokens) - width + 1))


def _feature_fields(features):
    fields = [features["title"], features["abstract"]]
    fields.extend(features["keywords"])
    fields.extend(concept["display_name"] for concept in features["concepts"])
    fields.extend(topic["display_name"] for topic in features["topics"])
    return [field for field in fields if field]


def _document_tokens(features):
    """Build the weighted token stream used by BM25 (title is repeated)."""
    tokens = _tokens(features["title"]) * 3
    tokens += _tokens(features["abstract"])
    for keyword in features["keywords"]:
        tokens += _tokens(keyword) * 2
    for concept in features["concepts"]:
        tokens += _tokens(concept["display_name"]) * 2
    for topic in features["topics"]:
        tokens += _tokens(topic["display_name"])
    return tokens


def _bm25(query_tokens, documents, k1=1.5, b=0.75):
    """Small deterministic BM25 implementation; no external dependencies."""
    if not query_tokens or not documents:
        return [0.0] * len(documents)
    query_tokens = list(dict.fromkeys(query_tokens))
    counts = []
    lengths = []
    document_frequency = {}
    for document in documents:
        frequencies = {}
        for token in document:
            frequencies[token] = frequencies.get(token, 0) + 1
        counts.append(frequencies)
        lengths.append(len(document))
        for token in frequencies:
            if token in query_tokens:
                document_frequency[token] = document_frequency.get(token, 0) + 1
    total = len(documents)
    average = sum(lengths) / total if total else 0.0
    scores = []
    for frequencies, length in zip(counts, lengths):
        score = 0.0
        for token in query_tokens:
            frequency = frequencies.get(token, 0)
            if not frequency:
                continue
            seen = document_frequency.get(token, 0)
            idf = math.log(1.0 + (total - seen + 0.5) / (seen + 0.5))
            denominator = frequency + k1 * (1.0 - b + b * (length / average
                                                           if average else 0.0))
            score += idf * (frequency * (k1 + 1.0)) / denominator
        scores.append(score)
    return scores


def _freshness_value(year, reference, half_life, time_filter):
    if year is None:
        return 0.0
    if isinstance(time_filter, dict):
        start = time_filter.get("start")
        end = time_filter.get("end")
        if (start is None or year >= start) and (end is None or year <= end):
            return 1.0
        distance = start - year if end is None else year - end
        return min(1.0, 0.5 ** (max(0, distance) / half_life))
    if reference is None:
        return 0.0
    return min(1.0, 0.5 ** (max(0, reference - year) / half_life))


def _expansion_edges(graph_context):
    """Return term -> best one-hop edge, preferring the expansion evidence."""
    edges = {}
    snapshot_assertions = None
    raw_edges = graph_context.get("edges") if isinstance(graph_context, dict) else None
    if isinstance(raw_edges, list):
        snapshot_assertions = {edge.get("assertion") for edge in raw_edges
                               if isinstance(edge, dict)}
    expansions = graph_context.get("expansions") if isinstance(graph_context, dict) else None
    for item in expansions or []:
        if not isinstance(item, dict):
            continue
        term = normalize(item.get("term", ""))
        edge = item.get("evidence")
        if not term or not isinstance(edge, dict) or not edge.get("assertion"):
            continue
        if snapshot_assertions is not None and edge["assertion"] not in snapshot_assertions:
            continue
        existing = edges.get(term)
        if existing is None or edge.get("weight", 0) > existing.get("weight", 0):
            edges[term] = edge
    return edges


class GraphRerankRanker:
    """Weighted graph/text/freshness reranker frozen as ``graph-rerank-v1``.

    Score components are normalized to ``[0, 1]``:

    * ``acemap``     = RRF fusion score / maximum fusion score;
    * ``graph``      = best one-hop expansion edge weight / maximum weight,
                       where the expansion term occurs in the paper;
    * ``text``       = BM25 over title/abstract/keywords/concepts/topics,
                       normalized by the best candidate score;
    * ``freshness``  = exponential decay from the newest candidate year, only
                       when the query asks for recent work (or config enables
                       it by default);
    * ``personalization`` = 0 in ``graph-rerank-v1`` (no profile input yet).
    """

    name = "graph_rerank"
    version = RANKER_VERSIONS["graph_rerank"]

    def __init__(self, config=None):
        self.config = ranking_config.resolve_for_ranker(
            config if config is not None else ranking_config.load_ranking_config(),
            "graph_rerank")
        self.weights = self.config["weights"]
        fusion = self.config["fusion"]
        self.k = fusion["k"]
        self.original_weight = fusion["original_weight"]
        self.expansion_weight = fusion["expansion_weight"]

    def rank(self, query, term_results, graph_context=None):
        graph_context = graph_context if isinstance(graph_context, dict) else {}
        ranked, metrics = rrf_merge(query, term_results, self.k,
                                    self.original_weight, self.expansion_weight)
        if not ranked:
            return {"results": [], "metrics": metrics}

        features = [paper_features(entry["paper"]) for entry in ranked]
        maximum_fusion = max((entry["fusion_score"] for entry in ranked), default=0.0)
        expansion_edges = _expansion_edges(graph_context)
        maximum_weight = max((edge.get("weight", 0.0) for edge in expansion_edges.values()),
                             default=0.0)

        topic = graph_context.get("topic") or query
        query_tokens = _tokens(topic)
        for term in expansion_edges:
            query_tokens += _tokens(term)
        text_scores = _bm25(query_tokens, [_document_tokens(item) for item in features])
        maximum_text = max(text_scores, default=0.0)
        reference_year = max((item["year"] for item in features if item["year"] is not None),
                             default=None)
        time_filter = graph_context.get("time_filter")
        freshness_enabled = (time_filter is not None
                             or self.config["freshness"]["enabled_by_default"])

        results = []
        for entry, item, text in zip(ranked, features, text_scores):
            acemap = entry["fusion_score"] / maximum_fusion if maximum_fusion > 0 else 0.0
            text_score = text / maximum_text if maximum_text > 0 else 0.0

            matched_graph_terms = []
            paths = []
            for term in entry["matches"]:
                matched_graph_terms.append(normalize(term["term"]))
            for term, edge in expansion_edges.items():
                if term not in matched_graph_terms and not any(
                        _phrase_in(_tokens(field), term) for field in _feature_fields(item)):
                    continue
                weight = float(edge.get("weight", 0.0))
                path_score = weight / maximum_weight if maximum_weight > 0 else 0.0
                path_score = min(1.0, max(0.0, path_score))
                paths.append({
                    "nodes": [query, term],
                    "relations": [edge["relation"]],
                    "weight": weight,
                    "score": path_score,
                    "assertions": [edge["assertion"]],
                })
            paths.sort(key=lambda path: (-path["score"], path["nodes"][1]))
            graph_score = max((path["score"] for path in paths), default=0.0)

            freshness = 0.0
            if freshness_enabled:
                freshness = _freshness_value(
                    item["year"], reference_year,
                    self.config["freshness"]["half_life_years"], time_filter)

            components = {
                "acemap": acemap,
                "graph": graph_score,
                "text": text_score,
                "freshness": freshness,
                "personalization": 0.0,
            }
            results.append(_v3_result(entry, components, self.weights, paths))

        results.sort(key=lambda item: (-item["score"], item["key"]))
        return {"results": results, "metrics": metrics}


RANKERS = {
    RRFRanker.name: RRFRanker,
    BaselineRanker.name: BaselineRanker,
    GraphRerankRanker.name: GraphRerankRanker,
}


def get_ranker(name="rrf", config=None, **kwargs):
    """Return a registered ranker by name; unknown names fail explicitly."""
    try:
        factory = RANKERS[name]
    except KeyError as exc:
        raise ValueError(f"Unknown ranker: {name}") from exc
    if name == "graph_rerank":
        return factory(config=config)
    if config is not None:
        kwargs.setdefault("config", config)
    return factory(**kwargs)
