"""Evaluation query, qrels, and configuration loaders.

Metrics are intentionally not implemented here; M1 adds them on top of these
validated inputs.  See docs/interface_protocol.md §9.
"""

from __future__ import annotations

from pathlib import Path

from .jsonio import read_json
from .text import normalize

QUERY_KEYS = ("id", "text", "topic", "time_filter", "task", "notes")
GRADES = (0, 1, 2, 3)


def load_queries(path):
    document = read_json(Path(path))
    if not isinstance(document, dict) or not isinstance(document.get("queries"), list):
        raise ValueError("Evaluation queries must be an object with a queries list")
    queries = []
    seen_ids = set()
    seen_texts = set()
    for item in document["queries"]:
        if not isinstance(item, dict):
            raise ValueError("Evaluation query must be an object")
        if set(item) != set(QUERY_KEYS):
            raise ValueError(f"Evaluation query keys must be exactly {sorted(QUERY_KEYS)}")
        identifier = item["id"]
        text = item["text"]
        if not isinstance(identifier, str) or not identifier:
            raise ValueError("Evaluation query id must be a non-empty string")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("Evaluation query text must be a non-empty string")
        normalized = normalize(text)
        if identifier in seen_ids:
            raise ValueError(f"Duplicate evaluation query id: {identifier}")
        if normalized in seen_texts:
            raise ValueError(f"Duplicate evaluation query text: {text}")
        seen_ids.add(identifier)
        seen_texts.add(normalized)
        if item["time_filter"] is not None and not isinstance(item["time_filter"], str):
            raise ValueError("Evaluation query time_filter must be null or a string")
        for key in ("topic", "task", "notes"):
            if not isinstance(item[key], str):
                raise ValueError(f"Evaluation query {key} must be a string")
        queries.append(item)
    return queries


def load_qrels(path, queries=None):
    document = read_json(Path(path))
    if not isinstance(document, dict):
        raise ValueError("qrels must be an object keyed by query id")
    known = {item["id"] for item in queries} if queries is not None else None
    if known is not None:
        unknown = set(document) - known
        if unknown:
            raise ValueError(f"qrels references unknown query ids: {sorted(unknown)}")
    for query_id, labels in document.items():
        if not isinstance(query_id, str) or not query_id:
            raise ValueError("qrels query id must be a non-empty string")
        if not isinstance(labels, dict):
            raise ValueError(f"qrels[{query_id}] must be an object")
        for doc_key, grade in labels.items():
            if not isinstance(doc_key, str) or not doc_key:
                raise ValueError("qrels doc_key must be a non-empty string")
            if isinstance(grade, bool) or grade not in GRADES:
                raise ValueError(f"qrels grade must be one of {GRADES}: {doc_key}")
    return document


def load_evaluation_config(path):
    document = read_json(Path(path))
    if not isinstance(document, dict):
        raise ValueError("Evaluation config must be an object")
    k_values = document.get("k_values")
    if (not isinstance(k_values, list) or not k_values
            or any(isinstance(value, bool) or not isinstance(value, int) or value < 1
                   for value in k_values)):
        raise ValueError("evaluation.k_values must be a non-empty list of positive integers")
    metrics = document.get("metrics")
    if (not isinstance(metrics, list) or not metrics
            or any(not isinstance(value, str) or not value for value in metrics)):
        raise ValueError("evaluation.metrics must be a non-empty list of strings")
    seed = document.get("seed")
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise ValueError("evaluation.seed must be an integer")
    splits = document.get("splits")
    if not isinstance(splits, dict) or not isinstance(splits.get("type"), str):
        raise ValueError("evaluation.splits must be an object with a type")
    return document


def coverage_report(queries, qrels):
    """Return label coverage without inventing relevance labels."""
    report = []
    for query in queries:
        labels = qrels.get(query["id"], {})
        positive = sum(grade > 0 for grade in labels.values())
        report.append({"id": query["id"], "text": query["text"],
                       "judged": len(labels), "positive": positive})
    return report
