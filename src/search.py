"""Query expansion, reciprocal-rank fusion, and result assembly."""

from __future__ import annotations

from .acemap_client import AcemapClient
from .conceptnet_provider import ConceptNet, SNAPSHOT_SCOPE
from .constants import ENDPOINT
from .edges import rank_neighbors
from .jsonio import write_json
from .rankers import rrf_merge
from .text import normalize
from .versions import OUTPUT_SCHEMA_V2


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


def run_search(args):
    """Run the dynamic ConceptNet pipeline for every configured query."""
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
