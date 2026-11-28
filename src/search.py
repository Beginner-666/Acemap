"""Query expansion, reciprocal-rank fusion, and result assembly."""

from __future__ import annotations

from . import config as ranking_config
from . import graph_index
from .acemap_client import AcemapClient
from .conceptnet_provider import ConceptNet, SNAPSHOT_SCOPE
from .constants import ENDPOINT
from .edges import rank_neighbors
from .jsonio import write_json
from .query_understanding import parse_intent, select_expansions
from .rankers import get_ranker, rrf_merge
from .text import normalize
from .versions import (OUTPUT_SCHEMA_V2, OUTPUT_SCHEMA_V3,
                       CONCEPTNET_ONLINE_SOURCE_ID, RECORD_FEATURE_VERSION,
                       SNAPSHOT_KEY_SCHEMA, config_hash)


def load_queries(args):
    """Read one query or a UTF-8 query file, dropping comments and duplicates."""
    queries = ([args.query] if args.query is not None else
               [line.strip() for line in args.queries.read_text(encoding="utf-8-sig").splitlines()
                if line.strip() and not line.lstrip().startswith("#")])
    queries = list(dict.fromkeys(normalize(query) for query in queries))
    if not queries or any(not query for query in queries):
        raise ValueError("Empty query")
    return queries


def search_one(client, query, size, expansion=None):
    """Search the original term and its expansions, then fuse by weighted RRF."""
    expansion = expansion or []
    terms = [query] + [item["term"] for item in expansion]
    term_results = []
    requests = []
    for term_index, term in enumerate(terms):
        try:
            records, cache_name = client.search(term, size)
        except (ValueError, OSError) as exc:
            term_results.append({"term": term, "is_original": term_index == 0,
                                 "status": "failed", "records": [],
                                 "cache_file": None, "error": str(exc)})
            requests.append({"term": term, "status": "failed", "error": str(exc)})
            continue
        term_results.append({"term": term, "is_original": term_index == 0,
                             "status": "ok", "records": records,
                             "cache_file": cache_name, "error": None})
        requests.append({"term": term, "status": "ok", "count": len(records),
                         "cache_file": cache_name})
    ranked, metrics = rrf_merge(query, term_results)
    failed = metrics["failed_requests"]
    status = "ok" if failed == 0 else ("failed" if requests[0]["status"] == "failed" else "partial")
    return {
        "query": query, "status": status,
        "enhancement_applied": bool(expansion), "expansions": expansion,
        "warning": None if expansion else "查询词没有图谱邻居，本次仅执行原始搜索",
        "requests": requests,
        "metrics": metrics,
        "results": ranked,
    }


def _collect_term_results(client, query, size, expansion, search_term=None):
    """Collect ordered term results and request records (v3 pipeline)."""
    original = search_term or query
    terms = [original] + [item["term"] for item in expansion]
    term_results = []
    requests = []
    for term_index, term in enumerate(terms):
        try:
            records, cache_name = client.search(term, size)
        except (ValueError, OSError) as exc:
            term_results.append({"term": term, "is_original": term_index == 0,
                                 "status": "failed", "records": [],
                                 "cache_file": None, "error": str(exc)})
            requests.append({"term": term, "status": "failed", "error": str(exc)})
            continue
        term_results.append({"term": term, "is_original": term_index == 0,
                             "status": "ok", "records": records,
                             "cache_file": cache_name, "error": None})
        requests.append({"term": term, "status": "ok", "count": len(records),
                         "cache_file": cache_name})
    return term_results, requests


def _document_graph_version(rows, local_graph_id=None):
    """Resolve the single graph version embedded in v3 settings.

    Mixing local graph versions in one document would make the inline settings
    lie about at least one row, so that case fails explicitly.  ``local_graph_id``
    is the provider's resolved id; using it keeps clean-v1 indexes from being
    mislabelled as raw-v1 while leaving the frozen snapshot shape untouched.
    """
    versions = set()
    for row in rows:
        snapshot = row["conceptnet"]
        backend = snapshot["backend"]
        if backend == "local":
            versions.add(local_graph_id or graph_index.derive_graph_id(snapshot["source"]))
        elif backend == "online":
            versions.add(CONCEPTNET_ONLINE_SOURCE_ID)
    if not versions:
        return "none"
    if len(versions) > 1:
        raise ValueError("v3 output cannot mix ConceptNet graph versions")
    return versions.pop()


def search_one_v3(client, query, size, expansion, ranker, graph_context,
                  search_term=None, baseline_only=False):
    """Search and rank one query through the v3 ranker interface.

    ``query`` is the frozen, normalized row query.  ``search_term`` is the
    effective retrieval term after query understanding; graph_rerank uses the
    parsed topic while baseline/rrf keep the full row query.
    """
    term_results, requests = _collect_term_results(
        client, query, size, expansion, search_term=search_term)
    ranked = ranker.rank(query, term_results, graph_context)
    results = ranked["results"]
    metrics = ranked["metrics"]
    failed = metrics["failed_requests"]
    status = ("ok" if failed == 0
              else ("failed" if requests[0]["status"] == "failed" else "partial"))
    if expansion:
        warning = None
    elif baseline_only:
        warning = "baseline ranker: original-term search only"
    else:
        warning = "查询词没有图谱邻居，本次仅执行原始搜索"
    return {
        "query": query, "status": status,
        "enhancement_applied": bool(expansion), "expansions": expansion,
        "warning": warning,
        "requests": requests,
        "metrics": metrics,
        "results": results,
    }


def _run_search_v3(args):
    """Run the opt-in schema-version-3 pipeline; v2 stays byte-identical."""
    queries = load_queries(args)
    ranker_name = getattr(args, "ranker", None) or "rrf"
    inline_config = getattr(args, "ranking_config_data", None)
    config = (inline_config if inline_config is not None
              else ranking_config.load_ranking_config(getattr(args, "ranking_config", None)))
    resolved = ranking_config.resolve_for_ranker(config, ranker_name)
    ranker = get_ranker(ranker_name, config=resolved)
    provider = ConceptNet(
        knowledge=args.knowledge,
        index=args.index,
        cache_dir=args.conceptnet_cache,
        timeout=args.timeout,
        refresh=args.refresh,
        cache_only=args.cache_only,
    )
    client = AcemapClient(args.cache, args.timeout, args.retries, args.cache_only, args.refresh)
    results = []
    for query in queries:
        intent = parse_intent(query)
        # graph_rerank uses the parsed topic as the effective retrieval term;
        # baseline/rrf keep the frozen v2 query semantics so rrf stays a control.
        search_term = ((intent["topic"] or query)
                       if ranker_name == "graph_rerank" else query)
        try:
            snapshot = provider.neighbors(search_term)
        except (ValueError, OSError) as error:
            snapshot = {"query": search_term, "matched_concept": None,
                        "forms_checked": [search_term], "backend": "none",
                        "source": None, "diagnostics": [str(error)],
                        "edges": [], "scope": SNAPSHOT_SCOPE}
        matched = snapshot["matched_concept"]
        degrees = {}
        if ranker_name == "baseline":
            # Baseline is the original-term control; do not spend expansion
            # requests or let their failures affect its status.
            expansion = []
        elif matched and ranker_name == "graph_rerank":
            # qu-v1 is part of the new graph reranker.  degree_cap uses the
            # global local index when available; otherwise the selector falls
            # back to the snapshot-local degree.
            if resolved["graph"]["degree_cap"] is not None:
                labels = {edge["subject"] for edge in snapshot["edges"]}
                labels.update(edge["object"] for edge in snapshot["edges"])
                degrees = graph_index.node_degrees(args.index, labels)
            expansion = select_expansions(
                snapshot["edges"], matched, args.max_expansions,
                min_edge_weight=resolved["graph"]["min_edge_weight"],
                degree_cap=resolved["graph"]["degree_cap"],
                relation_whitelist=resolved["graph"]["relation_whitelist"],
                degrees=degrees)
        elif matched:
            expansion = rank_neighbors(snapshot["edges"], matched, args.max_expansions)
        else:
            expansion = []
        expansion = [item for item in expansion
                     if item["term"] not in (query, search_term)]
        graph_context = {
            "graph_version": _document_graph_version(
                [{"conceptnet": snapshot}], local_graph_id=provider.graph_id),
            "edges": snapshot["edges"],
            "expansions": expansion,
            "topic": intent["topic"],
            "time_filter": intent["time_filter"],
            "search_term": search_term,
        }
        row = search_one_v3(
            client, query, args.size, expansion, ranker, graph_context,
            search_term=search_term, baseline_only=(ranker_name == "baseline"))
        row["conceptnet"] = snapshot
        if not expansion:
            if ranker_name == "baseline":
                row["warning"] = "baseline ranker: original-term search only"
            else:
                row["warning"] = (
                    "ConceptNet has no usable exact/normalized concept; original search only"
                    if snapshot["backend"] != "none"
                    else "ConceptNet unavailable; original search only")
        results.append(row)
        print(f"{query}: kg={snapshot['backend']}; concept={matched}; "
              f"expansions={[i['term'] for i in expansion]}; ranker={ranker.name}; "
              f"{row['status']}", flush=True)
    graph_version = _document_graph_version(results, local_graph_id=provider.graph_id)
    settings = {
        "knowledge": args.knowledge,
        "size_per_term": args.size,
        "max_expansions": args.max_expansions,
        "lexical_forms": "ai -> artificial intelligence; exact phrases only",
        "fusion": resolved["fusion"],
        "ranker": ranker.name,
        "ranker_version": ranker.version,
        "ranker_config": resolved,
        "config_hash": config_hash(resolved),
        "graph_version": graph_version,
        "snapshot_key_schema": SNAPSHOT_KEY_SCHEMA,
        "record_feature_version": RECORD_FEATURE_VERSION,
    }
    output = {"schema_version": OUTPUT_SCHEMA_V3, "domain": "query-time English ConceptNet",
              "endpoint": ENDPOINT, "settings": settings,
              "summary": {"queries": len(results),
                          "successful_queries": sum(r["status"] == "ok" for r in results),
                          "successful_with_enhancement": sum(
                              r["status"] == "ok" and r["enhancement_applied"] for r in results)},
              "searches": results}
    write_json(args.output, output)
    return 0 if all(r["status"] == "ok" and r["conceptnet"]["backend"] != "none"
                    for r in results) else 1


def run_search(args):
    """Run the dynamic ConceptNet pipeline for every configured query."""
    if getattr(args, "schema_version", OUTPUT_SCHEMA_V2) == OUTPUT_SCHEMA_V3:
        return _run_search_v3(args)
    queries = load_queries(args)
    provider = ConceptNet(
        knowledge=args.knowledge,
        index=args.index,
        cache_dir=args.conceptnet_cache,
        timeout=args.timeout,
        refresh=args.refresh,
        cache_only=args.cache_only,
    )
    client = AcemapClient(args.cache, args.timeout, args.retries, args.cache_only, args.refresh)
    results = []
    for query in queries:
        try:
            snapshot = provider.neighbors(query)
        except (ValueError, OSError) as error:
            # Failure paths must still emit a complete v2 snapshot object.
            snapshot = {"query": query, "matched_concept": None,
                        "forms_checked": [query], "backend": "none",
                        "source": None, "diagnostics": [str(error)],
                        "edges": [], "scope": SNAPSHOT_SCOPE}
        matched = snapshot["matched_concept"]
        expansion = (rank_neighbors(snapshot["edges"], matched, args.max_expansions)
                     if matched else [])
        expansion = [item for item in expansion if item["term"] != query]
        row = search_one(client, query, args.size, expansion=expansion)
        row["conceptnet"] = snapshot
        if not expansion:
            row["warning"] = ("ConceptNet has no usable exact/normalized concept; original search only"
                              if snapshot["backend"] != "none" else "ConceptNet unavailable; original search only")
        results.append(row)
        print(f"{query}: kg={snapshot['backend']}; concept={matched}; "
              f"expansions={[i['term'] for i in expansion]}; {row['status']}", flush=True)
    output = {"schema_version": OUTPUT_SCHEMA_V2, "domain": "query-time English ConceptNet", "endpoint": ENDPOINT,
              "settings": {"knowledge": args.knowledge, "size_per_term": args.size,
                           "max_expansions": args.max_expansions,
                           "lexical_forms": "ai -> artificial intelligence; exact phrases only",
                           "fusion": "weighted RRF: original=1, expansion=0.5, k=60"},
              "summary": {"queries": len(results),
                          "successful_queries": sum(r["status"] == "ok" for r in results),
                          "successful_with_enhancement": sum(
                              r["status"] == "ok" and r["enhancement_applied"] for r in results)},
              "searches": results}
    write_json(args.output, output)
    return 0 if all(r["status"] == "ok" and r["conceptnet"]["backend"] != "none"
                    for r in results) else 1
