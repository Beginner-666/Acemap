"""Version-aware validation and offline replay for search outputs.

v2 keeps the original byte-identical verification contract.  v3 schema
validation is implemented now; v3 replay arrives with the ranker milestone.
"""

from __future__ import annotations

import argparse
import math
from pathlib import Path
from unittest.mock import patch

from . import conceptnet_provider, constants, edges, jsonio, search, text
from .conceptnet_index import RELATIONS
from .conceptnet_provider import SNAPSHOT_SCOPE
from .versions import OUTPUT_SCHEMA_V2, OUTPUT_SCHEMA_V3, config_hash

TOP_KEYS = ("schema_version", "domain", "endpoint", "settings", "summary", "searches")
V2_SETTINGS_KEYS = ("knowledge", "size_per_term", "max_expansions",
                    "lexical_forms", "fusion")
V3_SETTINGS_KEYS = V2_SETTINGS_KEYS + (
    "ranker", "ranker_version", "ranker_config", "config_hash",
    "graph_version", "snapshot_key_schema", "record_feature_version")
SUMMARY_KEYS = ("queries", "successful_queries", "successful_with_enhancement")
ROW_KEYS = ("query", "status", "enhancement_applied", "expansions", "warning",
            "requests", "metrics", "results", "conceptnet")
METRIC_KEYS = ("baseline_unique", "enhanced_unique", "additional_unique",
               "failed_requests")
SNAPSHOT_KEYS = ("query", "matched_concept", "forms_checked", "backend",
                 "source", "diagnostics", "edges", "scope")
V2_RESULT_KEYS = ("key", "paper", "fusion_score", "matches")
V3_RESULT_KEYS = ("key", "paper", "score", "score_components", "evidence", "matches")
COMPONENT_KEYS = ("acemap", "graph", "text", "freshness", "personalization")
EVIDENCE_KEYS = ("matched_terms", "graph_paths", "matched_authors",
                 "matched_institutions")
PATH_KEYS = ("nodes", "relations", "weight", "score", "assertions")
SCOPE = SNAPSHOT_SCOPE
MANIFEST_KEYS = ("schema_version", "source_name", "source_url", "source_sha256",
                 "source_file", "rows_scanned", "edges_indexed", "language",
                 "relations", "min_weight")


def _keys(value, expected, context):
    if not isinstance(value, dict):
        raise ValueError(f"{context} must be an object")
    if list(value) != list(expected):
        raise ValueError(f"{context} fields must be exactly {list(expected)}")


def _number(value, context, minimum=None, maximum=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{context} must be a number")
    if not math.isfinite(float(value)):
        raise ValueError(f"{context} must be finite")
    if minimum is not None and value < minimum:
        raise ValueError(f"{context} must be >= {minimum}")
    if maximum is not None and value > maximum:
        raise ValueError(f"{context} must be <= {maximum}")
    return float(value)


def _integer(value, context, minimum=None):
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{context} must be an integer")
    if minimum is not None and value < minimum:
        raise ValueError(f"{context} must be >= {minimum}")
    return value


def _text(value, context):
    if not isinstance(value, str) or not value:
        raise ValueError(f"{context} must be a non-empty string")
    return value


def _string_list(value, context):
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"{context} must be a list of strings")
    return value


def _validate_settings(doc, version):
    settings = doc["settings"]
    _keys(settings, V2_SETTINGS_KEYS if version == 2 else V3_SETTINGS_KEYS,
          "settings")
    if settings["knowledge"] not in ("auto", "local", "online"):
        raise ValueError("settings.knowledge is invalid")
    _integer(settings["size_per_term"], "settings.size_per_term", 1)
    if settings["size_per_term"] > 100:
        raise ValueError("settings.size_per_term must be <= 100")
    _integer(settings["max_expansions"], "settings.max_expansions", 1)
    _text(settings["lexical_forms"], "settings.lexical_forms")
    if version == 2:
        if settings["fusion"] != "weighted RRF: original=1, expansion=0.5, k=60":
            raise ValueError("v2 settings.fusion is frozen")
        return None
    _keys(settings["fusion"], ("k", "original_weight", "expansion_weight"),
          "settings.fusion")
    _integer(settings["fusion"]["k"], "settings.fusion.k", 1)
    _number(settings["fusion"]["original_weight"], "settings.fusion.original_weight", 0)
    _number(settings["fusion"]["expansion_weight"], "settings.fusion.expansion_weight", 0)
    from .rankers import RANKER_VERSIONS
    if settings["ranker"] not in RANKER_VERSIONS:
        raise ValueError("settings.ranker is invalid")
    if settings["ranker_version"] != RANKER_VERSIONS[settings["ranker"]]:
        raise ValueError("settings.ranker_version does not match the implementation registry")
    _text(settings["ranker_version"], "settings.ranker_version")
    if settings["snapshot_key_schema"] != 2:
        raise ValueError("settings.snapshot_key_schema must be 2")
    if settings["record_feature_version"] != 1:
        raise ValueError("settings.record_feature_version must be 1")
    _text(settings["graph_version"], "settings.graph_version")
    from .config import validate_ranker_config
    validate_ranker_config(settings["ranker_config"], settings["ranker"])
    if config_hash(settings["ranker_config"]) != settings["config_hash"]:
        raise ValueError("settings.config_hash does not match ranker_config")
    if settings["fusion"] != settings["ranker_config"]["fusion"]:
        raise ValueError("settings.fusion must equal ranker_config.fusion")
    return settings


def _validate_summary(doc):
    summary = doc["summary"]
    _keys(summary, SUMMARY_KEYS, "summary")
    for key in SUMMARY_KEYS:
        _integer(summary[key], f"summary.{key}", 0)
    if summary["queries"] != len(doc["searches"]):
        raise ValueError("summary.queries does not match searches")
    successful = sum(row["status"] == "ok" for row in doc["searches"])
    enhanced = sum(row["status"] == "ok" and row["enhancement_applied"]
                   for row in doc["searches"])
    if summary["successful_queries"] != successful:
        raise ValueError("summary.successful_queries does not match searches")
    if summary["successful_with_enhancement"] != enhanced:
        raise ValueError("summary.successful_with_enhancement does not match searches")


def _validate_request(item):
    if not isinstance(item, dict):
        raise ValueError("requests[] item must be an object")
    _text(item.get("term"), "requests[].term")
    if item.get("status") == "ok":
        _keys(item, ("term", "status", "count", "cache_file"), "requests[].ok")
        _integer(item["count"], "requests[].count", 0)
        _text(item["cache_file"], "requests[].cache_file")
    elif item.get("status") == "failed":
        _keys(item, ("term", "status", "error"), "requests[].failed")
        _text(item["error"], "requests[].error")
    else:
        raise ValueError("requests[].status is invalid")


def _validate_edge(edge, backend):
    if not isinstance(edge, dict):
        raise ValueError("conceptnet.edges[] item must be an object")
    required = ("assertion", "relation", "subject", "object", "subject_uri",
                "object_uri", "weight", "metadata")
    for key in required:
        if key not in edge:
            raise ValueError(f"conceptnet edge is missing {key}")
    if backend == "local" and "source_line" not in edge:
        raise ValueError("local conceptnet edge is missing source_line")
    _text(edge["assertion"], "conceptnet edge assertion")
    _text(edge["relation"], "conceptnet edge relation")
    _text(edge["subject"], "conceptnet edge subject")
    _text(edge["object"], "conceptnet edge object")
    _text(edge["subject_uri"], "conceptnet edge subject_uri")
    _text(edge["object_uri"], "conceptnet edge object_uri")
    _number(edge["weight"], "conceptnet edge weight", 1)
    if edge["subject"] == edge["object"]:
        raise ValueError("conceptnet edge cannot be a self-loop")
    if edge["relation"] not in RELATIONS:
        raise ValueError("conceptnet edge relation is not permitted")
    if not isinstance(edge["metadata"], dict):
        raise ValueError("conceptnet edge metadata must be an object")


def _validate_snapshot(snapshot, query):
    _keys(snapshot, SNAPSHOT_KEYS, "conceptnet")
    if snapshot["query"] != query:
        raise ValueError("conceptnet.query does not match the search row")
    if snapshot["matched_concept"] is not None:
        _text(snapshot["matched_concept"], "conceptnet.matched_concept")
    _string_list(snapshot["forms_checked"], "conceptnet.forms_checked")
    if snapshot["backend"] not in ("online", "local", "none"):
        raise ValueError("conceptnet.backend is invalid")
    _string_list(snapshot["diagnostics"], "conceptnet.diagnostics")
    if snapshot["scope"] != SCOPE:
        raise ValueError("conceptnet.scope is frozen")
    if not isinstance(snapshot["edges"], list):
        raise ValueError("conceptnet.edges must be a list")
    if snapshot["backend"] == "none":
        if snapshot["source"] is not None or snapshot["edges"]:
            raise ValueError("backend=none must have no source or edges")
    else:
        if not isinstance(snapshot["source"], dict):
            raise ValueError("conceptnet.source must be an object")
        if snapshot["backend"] == "local":
            if list(snapshot["source"]) != list(MANIFEST_KEYS):
                raise ValueError("local conceptnet.source is not the frozen manifest")
    for edge in snapshot["edges"]:
        _validate_edge(edge, snapshot["backend"])
    ordered = [edges.edge_order(edge) for edge in snapshot["edges"]]
    if ordered != sorted(ordered):
        raise ValueError("conceptnet.edges are not in edge_order")


def _validate_matches(matches, request_counts):
    if not isinstance(matches, list) or not matches:
        raise ValueError("result.matches must be a non-empty list")
    for match in matches:
        _keys(match, ("term", "rank", "is_original"), "result.matches[]")
        _text(match["term"], "result.matches[].term")
        _integer(match["rank"], "result.matches[].rank", 1)
        if not isinstance(match["is_original"], bool):
            raise ValueError("result.matches[].is_original must be boolean")
        if match["term"] not in request_counts:
            raise ValueError("result.matches[] references a failed request")
        if match["rank"] > request_counts[match["term"]]:
            raise ValueError("result.matches[].rank exceeds the request count")


def _validate_v2_result(result, request_counts):
    _keys(result, V2_RESULT_KEYS, "result")
    _text(result["key"], "result.key")
    if not isinstance(result["paper"], dict):
        raise ValueError("result.paper must be an object")
    _number(result["fusion_score"], "result.fusion_score", 0)
    _validate_matches(result["matches"], request_counts)
    expected = sum(
        (1.0 if match["is_original"] else 0.5) / (60 + match["rank"])
        for match in result["matches"])
    if not math.isclose(result["fusion_score"], expected, rel_tol=0, abs_tol=1e-12):
        raise ValueError("result.fusion_score does not match its matches")


def _paper_evidence_tokens(paper):
    authors = set()
    institutions = set()
    for authorship in paper.get("authorships") or []:
        if not isinstance(authorship, dict):
            continue
        author = authorship.get("author")
        if isinstance(author, dict):
            for key in ("id", "display_name"):
                value = author.get(key)
                if isinstance(value, str) and value:
                    authors.add(value)
        for institution in authorship.get("institutions") or []:
            if isinstance(institution, dict):
                for key in ("id", "display_name"):
                    value = institution.get(key)
                    if isinstance(value, str) and value:
                        institutions.add(value)
    return authors, institutions


def _validate_v3_result(result, request_counts, settings):
    _keys(result, V3_RESULT_KEYS, "result")
    _text(result["key"], "result.key")
    if not isinstance(result["paper"], dict):
        raise ValueError("result.paper must be an object")
    _number(result["score"], "result.score", 0)
    _keys(result["score_components"], COMPONENT_KEYS, "result.score_components")
    components = {key: _number(result["score_components"][key],
                               f"result.score_components.{key}", 0, 1)
                  for key in COMPONENT_KEYS}
    weights = settings["ranker_config"]["weights"]
    expected = sum(weights[key] * components[key] for key in COMPONENT_KEYS)
    if not math.isclose(result["score"], expected, rel_tol=0, abs_tol=1e-9):
        raise ValueError("result.score does not match its components")
    if settings["ranker"] in ("baseline", "rrf"):
        if any(components[key] != 0.0 for key in ("graph", "text", "freshness",
                                                  "personalization")):
            raise ValueError(f"{settings['ranker']} results must only score acemap")
        if not math.isclose(result["score"], components["acemap"],
                            rel_tol=0, abs_tol=1e-12):
            raise ValueError(f"{settings['ranker']} score must equal acemap")
    if settings["ranker_config"]["personalization"]["alpha"] == 0.0:
        if components["personalization"] != 0.0:
            raise ValueError("personalization must be zero when alpha is zero")
    _validate_matches(result["matches"], request_counts)
    _keys(result["evidence"], EVIDENCE_KEYS, "result.evidence")
    _string_list(result["evidence"]["matched_terms"], "result.evidence.matched_terms")
    for path in result["evidence"]["graph_paths"]:
        _keys(path, PATH_KEYS, "result.evidence.graph_paths[]")
        _string_list(path["nodes"], "graph_path.nodes")
        if len(path["nodes"]) < 2:
            raise ValueError("graph_path.nodes must contain at least two nodes")
        _string_list(path["relations"], "graph_path.relations")
        if len(path["relations"]) != len(path["nodes"]) - 1:
            raise ValueError("graph_path.relations must match node count")
        _number(path["weight"], "graph_path.weight", 0)
        _number(path["score"], "graph_path.score", 0, 1)
        _string_list(path["assertions"], "graph_path.assertions")
        if not path["assertions"]:
            raise ValueError("graph_path.assertions must not be empty")
    author_tokens, institution_tokens = _paper_evidence_tokens(result["paper"])
    for key, tokens in (("matched_authors", author_tokens),
                        ("matched_institutions", institution_tokens)):
        if not isinstance(result["evidence"][key], list):
            raise ValueError(f"result.evidence.{key} must be a list")
        for item in result["evidence"][key]:
            _keys(item, ("id", "name"), f"result.evidence.{key}[]")
            if item["id"] is not None:
                _text(item["id"], f"result.evidence.{key}[].id")
            _text(item["name"], f"result.evidence.{key}[].name")
            candidates = {item["name"]}
            if item["id"] is not None:
                candidates.add(item["id"])
            if not candidates & tokens:
                raise ValueError(f"result.evidence.{key} is not traceable to the paper")


def _validate_rows(doc, version):
    if not isinstance(doc["searches"], list) or not doc["searches"]:
        raise ValueError("searches must be a non-empty list")
    seen_queries = set()
    for row in doc["searches"]:
        _keys(row, ROW_KEYS, "searches[] row")
        _text(row["query"], "searches[].query")
        if row["query"] in seen_queries:
            raise ValueError("searches[].query is duplicated")
        seen_queries.add(row["query"])
        if row["status"] not in ("ok", "partial", "failed"):
            raise ValueError("searches[].status is invalid")
        _keys(row["metrics"], METRIC_KEYS, "searches[].metrics")
        for key in METRIC_KEYS:
            _integer(row["metrics"][key], f"searches[].metrics.{key}", 0)
        if not isinstance(row["expansions"], list):
            raise ValueError("searches[].expansions must be a list")
        expansion_terms = []
        for expansion in row["expansions"]:
            _keys(expansion, ("term", "evidence", "domain_support_seeds"),
                  "searches[].expansions[]")
            _text(expansion["term"], "searches[].expansions[].term")
            expansion_terms.append(expansion["term"])
            if not isinstance(expansion["domain_support_seeds"], list):
                raise ValueError("domain_support_seeds must be a list")
        if len(expansion_terms) != len(set(expansion_terms)):
            raise ValueError("searches[].expansions terms must be unique")
        if expansion_terms and row["query"] in expansion_terms:
            raise ValueError("searches[].expansions must not repeat the query")
        if not isinstance(row["requests"], list):
            raise ValueError("searches[].requests must be a list")
        for request in row["requests"]:
            _validate_request(request)
        terms = [row["query"]] + expansion_terms
        if [request["term"] for request in row["requests"]] != terms:
            raise ValueError("requests must follow query then expansion order")
        request_counts = {request["term"]: request.get("count", 0)
                          for request in row["requests"] if request["status"] == "ok"}
        if row["status"] == "failed":
            if not row["requests"] or row["requests"][0]["status"] != "failed":
                raise ValueError("failed row must have a failed original request")
        elif row["status"] == "partial":
            if any(request["status"] != "ok" for request in row["requests"][:1]):
                raise ValueError("partial row must have a successful original request")
            if all(request["status"] == "ok" for request in row["requests"]):
                raise ValueError("partial row must contain a failed request")
        else:
            if any(request["status"] != "ok" for request in row["requests"]):
                raise ValueError("ok row must have only successful requests")
        if row["metrics"]["failed_requests"] != sum(
                request["status"] == "failed" for request in row["requests"]):
            raise ValueError("metrics.failed_requests does not match requests")
        if not isinstance(row["results"], list):
            raise ValueError("searches[].results must be a list")
        keys = []
        for result in row["results"]:
            if version == 2:
                _validate_v2_result(result, request_counts)
            else:
                _validate_v3_result(result, request_counts, doc["settings"])
            for match in result["matches"]:
                if match["is_original"] != (match["term"] == row["query"]):
                    raise ValueError("matches[].is_original disagrees with the query term")
            keys.append(result["key"])
        if len(keys) != len(set(keys)):
            raise ValueError("searches[].results keys must be unique")
        if row["metrics"]["enhanced_unique"] != len(keys):
            raise ValueError("metrics.enhanced_unique does not match results")
        baseline_keys = {result["key"] for result in row["results"]
                         if any(match["is_original"] for match in result["matches"])}
        if row["metrics"]["baseline_unique"] != len(baseline_keys):
            raise ValueError("metrics.baseline_unique does not match matches")
        if row["metrics"]["additional_unique"] != len(keys) - len(baseline_keys):
            raise ValueError("metrics.additional_unique does not match matches")
        _validate_snapshot(row["conceptnet"], row["query"])
        if version == 3:
            allowed_terms = {row["query"], *expansion_terms}
            assertion_ids = {edge["assertion"] for edge in row["conceptnet"]["edges"]}
            for result in row["results"]:
                if not set(result["evidence"]["matched_terms"]) <= allowed_terms:
                    raise ValueError("evidence.matched_terms is not a subset of query/expansions")
                for path in result["evidence"]["graph_paths"]:
                    if not set(path["assertions"]) <= assertion_ids:
                        raise ValueError("graph_path.assertions is not traceable to the snapshot")
        if row["enhancement_applied"] != bool(row["expansions"]):
            raise ValueError("enhancement_applied does not match expansions")
        if row["expansions"]:
            if row["warning"] is not None:
                raise ValueError("enhanced rows must not have a warning")
        else:
            expected = ("ConceptNet has no usable exact/normalized concept; original search only"
                        if row["conceptnet"]["backend"] != "none"
                        else "ConceptNet unavailable; original search only")
            if row["warning"] != expected:
                raise ValueError("warning does not match the ConceptNet backend")
        if version == 2:
            ordered = [(-result["fusion_score"], result["key"]) for result in row["results"]]
        else:
            ordered = [(-result["score"], result["key"]) for result in row["results"]]
        if ordered != sorted(ordered):
            raise ValueError("results are not sorted by score then key")


def validate_v2_document(doc):
    if not isinstance(doc, dict):
        raise ValueError("Search output must be an object")
    _keys(doc, TOP_KEYS, "document")
    if doc["schema_version"] != OUTPUT_SCHEMA_V2:
        raise ValueError("Expected current dynamic-search schema version 2")
    if doc["domain"] != "query-time English ConceptNet":
        raise ValueError("v2 domain is frozen")
    if doc["endpoint"] != constants.ENDPOINT:
        raise ValueError("v2 endpoint does not match the client")
    _validate_settings(doc, 2)
    _validate_summary(doc)
    _validate_rows(doc, 2)
    return doc


def validate_v3_document(doc):
    if not isinstance(doc, dict):
        raise ValueError("Search output must be an object")
    _keys(doc, TOP_KEYS, "document")
    if doc["schema_version"] != OUTPUT_SCHEMA_V3:
        raise ValueError("Expected search output schema version 3")
    if doc["domain"] != "query-time English ConceptNet":
        raise ValueError("v3 domain is frozen")
    if doc["endpoint"] != constants.ENDPOINT:
        raise ValueError("v3 endpoint does not match the client")
    settings = _validate_settings(doc, 3)
    if not isinstance(settings["ranker_config"], dict):
        raise ValueError("settings.ranker_config must be an object")
    _validate_summary(doc)
    _validate_rows(doc, 3)
    return doc


def _expected_queries(path):
    return list(dict.fromkeys(text.normalize(line) for line in
                Path(path).read_text(encoding="utf-8-sig").splitlines()
                if line.strip() and not line.lstrip().startswith("#")))


def verify_v2(args, original):
    validate_v2_document(original)
    expected = _expected_queries(args.queries)
    if [row["query"] for row in original["searches"]] != expected:
        raise ValueError("Results do not cover the configured queries")
    settings = original["settings"]
    replay_path = args.results.with_name(args.results.stem + "_replay.json")
    replay_args = argparse.Namespace(
        knowledge=settings["knowledge"], index=args.index,
        conceptnet_cache=args.conceptnet_cache, cache=args.cache,
        output=replay_path, query=None, queries=args.queries,
        max_expansions=settings["max_expansions"], size=settings["size_per_term"],
        timeout=20, retries=0, cache_only=True, refresh=False,
    )
    provider = conceptnet_provider.ConceptNet(
        knowledge=replay_args.knowledge, index=replay_args.index,
        cache_dir=replay_args.conceptnet_cache, timeout=replay_args.timeout,
        refresh=replay_args.refresh, cache_only=replay_args.cache_only,
    )
    with patch("src.net.urlopen", side_effect=AssertionError("network forbidden")):
        for row in original["searches"]:
            snapshot = provider.neighbors(row["query"])
            if snapshot != row["conceptnet"] or snapshot["backend"] == "none":
                raise ValueError("Saved ConceptNet evidence does not match cache")
            matched = snapshot["matched_concept"]
            expansions = (edges.rank_neighbors(snapshot["edges"], matched,
                                               settings["max_expansions"]) if matched else [])
            expansions = [item for item in expansions if item["term"] != row["query"]]
            if row["expansions"] != expansions or row["enhancement_applied"] != bool(expansions):
                raise ValueError("Expansion or enhancement status mismatch")
            if row["status"] != "ok" or any(item["status"] != "ok" for item in row["requests"]):
                raise ValueError("Failed search request")
            keys = [item["key"] for item in row["results"]]
            if not keys or len(keys) != len(set(keys)):
                raise ValueError("Empty or duplicate result keys")
        if search.run_search(replay_args) != 0:
            raise ValueError("Replay failed")
    if args.results.read_bytes() != replay_path.read_bytes():
        raise ValueError("Replay is not byte-identical")
    report = {
        "passed": True, "byte_identical": True, "network_forbidden": True,
        "index_bytes": args.index.stat().st_size if args.index.exists() else None,
        "queries": [{"query": row["query"], "backend": row["conceptnet"]["backend"],
                     "matched_concept": row["conceptnet"]["matched_concept"],
                     "enhancement_applied": row["enhancement_applied"],
                     "expansion_terms": [item["term"] for item in row["expansions"]],
                     **row["metrics"], "diagnostics": row["conceptnet"]["diagnostics"]}
                    for row in original["searches"]],
        "sha256": jsonio.digest(args.results),
    }
    jsonio.write_json(args.results.with_name("dynamic_verification.json"), report)
    print("Verification passed: saved evidence checked; both networks forbidden; replay byte-identical.")
    return 0


def verify_v3(args, original):
    """Validate and replay v3 using only the inline ranker configuration."""
    validate_v3_document(original)
    expected = _expected_queries(args.queries)
    if [row["query"] for row in original["searches"]] != expected:
        raise ValueError("Results do not cover the configured queries")
    settings = original["settings"]
    replay_path = args.results.with_name(args.results.stem + "_replay.json")
    replay_args = argparse.Namespace(
        knowledge=settings["knowledge"], index=args.index,
        conceptnet_cache=args.conceptnet_cache, cache=args.cache,
        output=replay_path, query=None, queries=args.queries,
        max_expansions=settings["max_expansions"], size=settings["size_per_term"],
        timeout=20, retries=0, cache_only=True, refresh=False,
        schema_version=3, ranker=settings["ranker"],
        ranking_config=None, ranking_config_data=settings["ranker_config"],
    )
    with patch("src.net.urlopen", side_effect=AssertionError("network forbidden")):
        if search.run_search(replay_args) != 0:
            raise ValueError("v3 replay failed")
    if args.results.read_bytes() != replay_path.read_bytes():
        raise ValueError("v3 replay is not byte-identical")
    report = {
        "passed": True, "byte_identical": True, "network_forbidden": True,
        "ranker": settings["ranker"],
        "ranker_version": settings["ranker_version"],
        "graph_version": settings["graph_version"],
        "config_hash": settings["config_hash"],
        "index_bytes": args.index.stat().st_size if args.index.exists() else None,
        "queries": [{
            "query": row["query"], "backend": row["conceptnet"]["backend"],
            "matched_concept": row["conceptnet"]["matched_concept"],
            "enhancement_applied": row["enhancement_applied"],
            "expansion_terms": [item["term"] for item in row["expansions"]],
            **row["metrics"],
            "diagnostics": row["conceptnet"]["diagnostics"],
        } for row in original["searches"]],
        "sha256": jsonio.digest(args.results),
    }
    jsonio.write_json(args.results.with_name("dynamic_verification_v3.json"), report)
    print("Verification passed: v3 schema checked; inline config replayed; "
          "both networks forbidden; replay byte-identical.")
    return 0


def build_parser():
    parser = argparse.ArgumentParser(
        description="Verify current dynamic searches and replay with both network paths forbidden.")
    parser.add_argument("--results", type=Path,
                        default=constants.ROOT / "results/dynamic_queries.json")
    parser.add_argument("--queries", type=Path,
                        default=constants.ROOT / "config/dynamic_queries.txt")
    parser.add_argument("--cache", type=Path, default=constants.DEFAULT_ACEAMP_CACHE)
    parser.add_argument("--conceptnet-cache", type=Path,
                        default=constants.DEFAULT_CONCEPTNET_CACHE)
    parser.add_argument("--index", type=Path, default=constants.DEFAULT_INDEX)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    original = jsonio.read_json(args.results)
    if not isinstance(original, dict):
        raise ValueError("Search output must be an object")
    version = original.get("schema_version")
    if version == OUTPUT_SCHEMA_V2:
        return verify_v2(args, original)
    if version == OUTPUT_SCHEMA_V3:
        return verify_v3(args, original)
    raise ValueError("Unknown search output schema version")
