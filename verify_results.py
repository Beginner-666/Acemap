"""Verify current dynamic searches and replay with both network paths forbidden."""

import argparse
from pathlib import Path
import sys
from unittest.mock import patch

import acemap_search as app
import conceptnet_dynamic as dynamic


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", type=Path, default=app.ROOT / "results/dynamic_queries.json")
    parser.add_argument("--queries", type=Path, default=app.ROOT / "config/dynamic_queries.txt")
    parser.add_argument("--cache", type=Path, default=app.ROOT / "data/acemap_cache")
    parser.add_argument("--conceptnet-cache", type=Path, default=app.ROOT / "data/conceptnet_cache")
    parser.add_argument("--index", type=Path, default=app.ROOT / "data/conceptnet_en.sqlite")
    args = parser.parse_args()
    original = app.read_json(args.results)
    if original.get("schema_version") != 2:
        raise ValueError("Expected current dynamic-search schema version 2")
    expected = list(dict.fromkeys(app.normalize(line) for line in
                    args.queries.read_text(encoding="utf-8-sig").splitlines()
                    if line.strip() and not line.lstrip().startswith("#")))
    if [row["query"] for row in original["searches"]] != expected:
        raise ValueError("Results do not cover the configured queries")
    settings = original["settings"]
    replay_path = args.results.with_name(args.results.stem + "_replay.json")
    replay_args = argparse.Namespace(
        knowledge=settings["knowledge"], index=args.index, conceptnet_cache=args.conceptnet_cache,
        cache=args.cache, output=replay_path, query=None, queries=args.queries,
        max_expansions=settings["max_expansions"], size=settings["size_per_term"],
        timeout=20, retries=0, cache_only=True, refresh=False,
    )
    provider = dynamic.ConceptNet(replay_args)
    with patch.object(app, "urlopen", side_effect=AssertionError("Acemap network forbidden")), \
         patch.object(dynamic, "urlopen", side_effect=AssertionError("ConceptNet network forbidden")):
        for row in original["searches"]:
            snapshot = provider.neighbors(row["query"])
            if snapshot != row["conceptnet"] or snapshot["backend"] == "none":
                raise ValueError("Saved ConceptNet evidence does not match cache")
            matched = snapshot["matched_concept"]
            expansions = (app.expand({"edges": snapshot["edges"]}, matched,
                                     settings["max_expansions"]) if matched else [])
            expansions = [item for item in expansions if item["term"] != row["query"]]
            if row["expansions"] != expansions or row["enhancement_applied"] != bool(expansions):
                raise ValueError("Expansion or enhancement status mismatch")
            if row["status"] != "ok" or any(item["status"] != "ok" for item in row["requests"]):
                raise ValueError("Failed search request")
            keys = [item["key"] for item in row["results"]]
            if not keys or len(keys) != len(set(keys)):
                raise ValueError("Empty or duplicate result keys")
        if app.run_search(replay_args) != 0:
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
        "sha256": app.digest(args.results),
    }
    app.write_json(args.results.with_name("dynamic_verification.json"), report)
    print("Verification passed: saved evidence checked; both networks forbidden; replay byte-identical.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, OSError, KeyError) as error:
        print(f"Verification failed: {error}", file=sys.stderr)
        sys.exit(1)
