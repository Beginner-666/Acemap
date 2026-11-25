"""Evaluation query, qrels, metric, and report interfaces.

The loaders are intentionally strict: an evaluation run must fail explicitly
when a query set, qrels file, result document, or ranker name is inconsistent.
Metrics are computed only from the supplied qrels; unjudged documents count as
grade 0 and are reported separately.  See docs/interface_protocol.md §9.
"""

from __future__ import annotations

import math
from pathlib import Path

from . import verification
from .jsonio import digest, read_json, write_json
from .text import normalize

QUERY_KEYS = ("id", "text", "topic", "time_filter", "task", "notes")
GRADES = (0, 1, 2, 3)
METRIC_NAMES = ("precision", "recall", "ndcg", "mrr")
REPORT_SCHEMA_VERSION = "evaluation-v1"
DEFAULT_RESULTS = {
    "baseline": "results/eval_baseline.json",
    "rrf": "results/eval_rrf.json",
    "graph_rerank": "results/eval_graph_rerank.json",
}


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
    unknown = set(metrics) - set(METRIC_NAMES)
    if unknown:
        raise ValueError(f"Unknown evaluation metrics: {sorted(unknown)}")
    seed = document.get("seed")
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise ValueError("evaluation.seed must be an integer")
    splits = document.get("splits")
    if not isinstance(splits, dict) or not isinstance(splits.get("type"), str):
        raise ValueError("evaluation.splits must be an object with a type")
    threshold = document.get("relevance_threshold", 1)
    if (isinstance(threshold, bool) or not isinstance(threshold, int)
            or threshold not in (1, 2, 3)):
        raise ValueError("evaluation.relevance_threshold must be 1, 2 or 3")
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


def _gain(grade):
    return (2 ** grade) - 1


def _dcg(grades):
    return sum(_gain(grade) / math.log2(index + 2)
               for index, grade in enumerate(grades))


def query_metrics(ranked_keys, labels, k_values, relevance_threshold=1):
    """Compute precision/recall/nDCG/MRR at every requested cut-off.

    ``ranked_keys`` must be ordered best first.  Only documents present in
    ``labels`` have a grade; unjudged documents contribute no gain and are
    counted in ``unjudged@k``.
    """
    positive = {key for key, grade in labels.items()
                if grade >= relevance_threshold}
    total_positive = len(positive)
    metrics = {}
    for k in k_values:
        top = list(ranked_keys[:k])
        hits = sum(key in positive for key in top)
        metrics[f"precision@{k}"] = hits / k if k else 0.0
        metrics[f"recall@{k}"] = (hits / total_positive) if total_positive else 0.0
        graded = [labels.get(key, 0) for key in top]
        ideal = sorted(labels.values(), reverse=True)[:k]
        ideal_dcg = _dcg(ideal)
        metrics[f"ndcg@{k}"] = _dcg(graded) / ideal_dcg if ideal_dcg > 0 else 0.0
        reciprocal = 0.0
        for rank, key in enumerate(top, 1):
            if key in positive:
                reciprocal = 1.0 / rank
                break
        metrics[f"mrr@{k}"] = reciprocal
        metrics[f"unjudged@{k}"] = sum(key not in labels for key in top)
    return metrics


def _mean(values):
    values = list(values)
    return sum(values) / len(values) if values else 0.0


def _result_keys(row):
    return [result["key"] for result in row["results"]]


def evaluate_documents(queries, qrels, documents, config, sources=None):
    """Evaluate validated result documents and return a deterministic report."""
    sources = sources or {}
    k_values = config["k_values"]
    threshold = config.get("relevance_threshold", 1)
    query_texts = [normalize(item["text"]) for item in queries]
    report_rankers = {}
    for name, document in documents.items():
        rows = document.get("searches")
        if not isinstance(rows, list):
            raise ValueError(f"{name} result document is missing searches")
        by_query = {row["query"]: row for row in rows}
        if set(by_query) != set(query_texts):
            raise ValueError(f"{name} result document does not cover the evaluation queries")
        per_query = []
        for query, text in zip(queries, query_texts):
            row = by_query[text]
            labels = qrels.get(query["id"])
            if not labels:
                raise ValueError(f"Missing qrels for query {query['id']}")
            if not any(grade >= threshold for grade in labels.values()):
                raise ValueError(
                    f"qrels for query {query['id']} have no grade >= {threshold}")
            per_query.append({
                "id": query["id"],
                "text": text,
                "metrics": query_metrics(_result_keys(row), labels, k_values, threshold),
                "results": len(row["results"]),
            })
        metric_names = sorted(per_query[0]["metrics"]) if per_query else []
        aggregate = {metric: _mean(item["metrics"][metric] for item in per_query)
                     for metric in metric_names}
        report_rankers[name] = {
            "source": str(sources.get(name, "")),
            "schema_version": document.get("schema_version"),
            "ranker": document.get("settings", {}).get("ranker")
                       if isinstance(document.get("settings"), dict) else None,
            "queries": len(per_query),
            "metrics": aggregate,
            "per_query": per_query,
        }
    comparison = {}
    if "baseline" in report_rankers:
        baseline_metrics = report_rankers["baseline"]["metrics"]
        for name, entry in report_rankers.items():
            if name == "baseline":
                continue
            deltas = {}
            for metric, value in entry["metrics"].items():
                base = baseline_metrics.get(metric, 0.0)
                deltas[metric] = {
                    "baseline": base,
                    "ranker": value,
                    "delta": value - base,
                    "relative": ((value - base) / base) if base else None,
                }
            comparison[name] = deltas
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "config": config,
        "queries": {"count": len(query_texts),
                    "file": str(sources.get("queries", "")),
                    "sha256": sources.get("queries_sha256")},
        "qrels": {
            "file": str(sources.get("qrels", "")),
            "sha256": sources.get("qrels_sha256"),
            "labels": sum(len(labels) for labels in qrels.values()),
            "queries": len(qrels),
            "relevance_threshold": threshold,
            "provenance": sources.get("qrels_provenance"),
        },
        "rankers": report_rankers,
        "comparison": comparison,
    }


def _parse_result_specs(specs):
    if not specs:
        return {name: Path(path) for name, path in DEFAULT_RESULTS.items()}
    parsed = {}
    for spec in specs:
        if "=" not in spec:
            raise ValueError("--results must use NAME=PATH")
        name, path = spec.split("=", 1)
        if not name or not path or name in parsed:
            raise ValueError(f"Invalid or duplicate --results entry: {spec}")
        parsed[name] = Path(path)
    missing = {"baseline", "graph_rerank"} - set(parsed)
    if missing:
        raise ValueError(f"Evaluation requires baseline and graph_rerank results: {sorted(missing)}")
    return parsed


def _load_result_document(name, path):
    document = read_json(path)
    version = document.get("schema_version") if isinstance(document, dict) else None
    if version == 2:
        verification.validate_v2_document(document)
    elif version == 3:
        verification.validate_v3_document(document)
        ranker = document["settings"]["ranker"]
        if name in ("baseline", "rrf", "graph_rerank") and ranker != name:
            raise ValueError(
                f"{path} was generated by {ranker}, not by {name}")
    else:
        raise ValueError(f"{path} is not a v2 or v3 result document")
    return document


def run_evaluation(args):
    """Run the ``evaluate`` command over saved result documents."""
    queries = load_queries(args.queries)
    qrels = load_qrels(args.qrels, queries)
    config = load_evaluation_config(args.config)
    paths = _parse_result_specs(getattr(args, "results", None))
    documents = {}
    sources = {}
    for name, path in paths.items():
        documents[name] = _load_result_document(name, path)
        sources[name] = path
    sources["queries"] = args.queries
    sources["queries_sha256"] = digest(args.queries)
    sources["qrels"] = args.qrels
    sources["qrels_sha256"] = digest(args.qrels)
    provenance = getattr(args, "qrels_provenance", None)
    if provenance is not None:
        sources["qrels_provenance"] = provenance
    report = evaluate_documents(queries, qrels, documents, config, sources)
    write_json(args.output, report)
    baseline = report["rankers"]["baseline"]["metrics"]
    for name, entry in report["rankers"].items():
        print(f"{name}: " + " ".join(
            f"{metric}={value:.4f}" for metric, value in entry["metrics"].items()
            if metric.startswith(("ndcg@", "recall@", "mrr@"))), flush=True)
    if "graph_rerank" in report["rankers"]:
        for metric in ("ndcg@10", "recall@10"):
            if metric in baseline:
                value = report["rankers"]["graph_rerank"]["metrics"][metric]
                print(f"graph_rerank vs baseline {metric}: "
                      f"{baseline[metric]:.4f} -> {value:.4f} "
                      f"({value - baseline[metric]:+.4f})", flush=True)
    return 0
