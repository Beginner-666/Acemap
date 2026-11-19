"""Query expansion, reciprocal-rank fusion, and result assembly."""

from __future__ import annotations

from .acemap_client import AcemapClient
from .conceptnet_provider import ConceptNet
from .constants import ENDPOINT
from .edges import rank_neighbors
from .jsonio import write_json
from .records import identity
from .text import normalize


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
    merged = {}
    requests = []
    baseline_ids = set()
    for term_index, term in enumerate(terms):
        try:
            records, cache_name = client.search(term, size)
        except (ValueError, OSError) as exc:
            requests.append({"term": term, "status": "failed", "error": str(exc)})
            continue
        requests.append({"term": term, "status": "ok", "count": len(records),
                         "cache_file": cache_name})
        seen_in_query = set()
        for rank, record in enumerate(records, 1):
            key = identity(record)
            if key in seen_in_query:
                continue
            seen_in_query.add(key)
            if term_index == 0:
                baseline_ids.add(key)
            if key not in merged:
                merged[key] = {"key": key, "paper": record, "fusion_score": 0.0, "matches": []}
            item = merged[key]
            # Weighted reciprocal rank fusion; original query receives more weight.
            item["fusion_score"] += (1.0 if term_index == 0 else 0.5) / (60 + rank)
            item["matches"].append({"term": term, "rank": rank, "is_original": term_index == 0})
    failed = sum(item["status"] == "failed" for item in requests)
    ranked = sorted(merged.values(), key=lambda item: (-item["fusion_score"], item["key"]))
    status = "ok" if failed == 0 else ("failed" if requests[0]["status"] == "failed" else "partial")
    return {
        "query": query, "status": status,
        "enhancement_applied": bool(expansion), "expansions": expansion,
        "warning": None if expansion else "查询词没有图谱邻居，本次仅执行原始搜索",
        "requests": requests,
        "metrics": {"baseline_unique": len(baseline_ids), "enhanced_unique": len(merged),
                    "additional_unique": len(set(merged) - baseline_ids),
                    "failed_requests": failed},
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
            snapshot = {"query": query, "matched_concept": None, "backend": "none",
                        "edges": [], "diagnostics": [str(error)]}
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
    output = {"schema_version": 2, "domain": "query-time English ConceptNet", "endpoint": ENDPOINT,
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
