"""Opt-in one-hop author/institution expansion over a first-round result set.

Phase-one multi-hop is deliberately a separate command and output schema
(``multihop-v1``): the frozen v2/v3 search documents do not have a field for a
``query -> paper -> author -> paper`` path, so adding one to them would break
the interface contract.  This module never runs unless the user invokes the
``multihop`` command, and it never modifies the input document.

See docs/interface_protocol.md §4.10.
"""

from __future__ import annotations

import math
from pathlib import Path

from . import constants
from .acemap_client import AcemapClient
from .jsonio import digest, read_json, write_json
from .paper_features import paper_features
from .query_understanding import parse_intent
from .records import identity
from .text import normalize

MULTIHOP_SCHEMA_VERSION = "multihop-v1"
MULTIHOP_RULES_VERSION = "multihop-v1"

CONFIG_KEYS = ("hop_weight", "max_authors", "max_institutions", "seed_limit",
               "size_per_hop", "min_seed_score", "k")
DEFAULT_CONFIG = {
    "hop_weight": 0.25,
    "max_authors": 2,
    "max_institutions": 2,
    "seed_limit": 5,
    "size_per_hop": 10,
    "min_seed_score": 0.65,
    "k": 60,
}


def _require_keys(value, keys, context):
    if not isinstance(value, dict) or set(value) != set(keys):
        raise ValueError(f"{context} keys must be exactly {sorted(keys)}")


def _number(value, context, minimum=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{context} must be a number")
    if not math.isfinite(float(value)):
        raise ValueError(f"{context} must be finite")
    if minimum is not None and value < minimum:
        raise ValueError(f"{context} must be >= {minimum}")
    return float(value)


def validate_config(config):
    _require_keys(config, CONFIG_KEYS, "multihop config")
    for key in ("max_authors", "max_institutions", "seed_limit", "size_per_hop"):
        value = config[key]
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"multihop config.{key} must be an integer >= 1")
    if config["size_per_hop"] > 100:
        raise ValueError("multihop config.size_per_hop must be <= 100")
    if (isinstance(config["hop_weight"], bool)
            or not isinstance(config["hop_weight"], (int, float))
            or not 0 <= config["hop_weight"] <= 1):
        raise ValueError("multihop config.hop_weight must be a number in [0, 1]")
    _number(config["min_seed_score"], "multihop config.min_seed_score", 0)
    if isinstance(config["k"], bool) or not isinstance(config["k"], int) or config["k"] < 1:
        raise ValueError("multihop config.k must be an integer >= 1")
    return config


def load_config(path=None):
    path = Path(path) if path is not None else constants.ROOT / "config" / "multihop.json"
    if path.exists():
        value = read_json(path)
    else:
        value = dict(DEFAULT_CONFIG)
    return validate_config(value)


def _base_score(result):
    if "score" in result:
        return float(result["score"])
    return float(result.get("fusion_score", 0.0))


def _seed_term(row, result, expansion_weights):
    candidates = [match for match in result.get("matches", [])
                  if not match.get("is_original")
                  and match.get("term") in expansion_weights]
    if candidates:
        candidates.sort(key=lambda match: (-expansion_weights[match["term"]],
                                           match["rank"], match["term"]))
        return normalize(candidates[0]["term"])
    return normalize(row["query"])


def _collect_seeds(row, config):
    results = row["results"]
    if not results:
        return [], 0.0, "no first-round results"
    top_score = _base_score(results[0])
    if top_score <= 0 or top_score < config["min_seed_score"]:
        return [], top_score, "first-round relevance below the configured threshold"
    expansion_weights = {}
    for item in row.get("expansions", []):
        evidence = item.get("evidence") or {}
        expansion_weights[normalize(item["term"])] = float(evidence.get("weight", 0.0))
    candidates = {}
    for rank, result in enumerate(results[:config["seed_limit"]], 1):
        features = paper_features(result["paper"])
        weight = 1.0 / (config["k"] + rank)
        term = _seed_term(row, result, expansion_weights)
        title = features["title"] or result["key"]
        for kind, items in (("author", features["authors"]),
                            ("institution", features["institutions"])):
            for item in items:
                identifier = item.get("id")
                name = item.get("display_name")
                if not identifier or not name:
                    continue
                token = (kind, identifier)
                current = candidates.get(token)
                record = {
                    "type": kind, "id": identifier, "name": name,
                    "seed_key": result["key"], "seed_rank": rank,
                    "seed_title": title, "term": term, "weight": weight,
                }
                if current is None or (-record["weight"], record["seed_key"]) < (
                        -current["weight"], current["seed_key"]):
                    candidates[token] = record
    authors = sorted((item for item in candidates.values() if item["type"] == "author"),
                     key=lambda item: (-item["weight"], item["id"]))[:config["max_authors"]]
    institutions = sorted(
        (item for item in candidates.values() if item["type"] == "institution"),
        key=lambda item: (-item["weight"], item["id"]))[:config["max_institutions"]]
    seeds = authors + institutions
    if not seeds:
        return [], top_score, "no author or institution identifiers in first-round results"
    return seeds, top_score, "triggered"


def _path(seed, term, query, result, rank, score):
    return {
        "query": query,
        "term": term,
        "seed_key": seed["seed_key"],
        "seed_title": seed["seed_title"],
        "node": {"type": seed["type"], "id": seed["id"], "name": seed["name"]},
        "paper_key": identity(result),
        "rank": rank,
        "score": score,
    }


def expand_query(row, client, config):
    """Expand one first-round query with at most one author/institution hop."""
    query = row["query"]
    intent = parse_intent(query)
    seeds, top_score, reason = _collect_seeds(row, config)
    base = []
    for result in row["results"]:
        base.append({
            "key": result["key"], "paper": result["paper"],
            "base_score": _base_score(result), "multihop_score": 0.0,
            "first_round": True, "from_multihop": False, "sources": [],
        })
    requests = []
    if not seeds:
        for item in base:
            item["score"] = item["base_score"]
        base.sort(key=lambda item: (-item["score"], item["key"]))
        return {
            "query": query, "status": "skipped",
            "trigger": {"applied": False, "reason": reason,
                        "top_score": top_score,
                        "min_seed_score": config["min_seed_score"]},
            "intent": intent,
            "seeds": [],
            "requests": [],
            "results": base,
        }
    keyword = intent["topic"] or query
    for seed in seeds:
        request = {"type": seed["type"], "id": seed["id"], "name": seed["name"],
                   "term": keyword}
        try:
            if seed["type"] == "author":
                records, cache_name = client.search_filtered(
                    keyword, config["size_per_hop"], author_ids=[seed["id"]])
            else:
                records, cache_name = client.search_filtered(
                    keyword, config["size_per_hop"], institution_ids=[seed["id"]])
        except (ValueError, OSError) as error:
            requests.append({**request, "status": "failed", "error": str(error)})
            continue
        requests.append({**request, "status": "ok", "count": len(records),
                         "cache_file": cache_name})
        seen = set()
        for rank, result in enumerate(records, 1):
            key = identity(result)
            if key in seen:
                continue
            seen.add(key)
            score = (config["hop_weight"] * seed["weight"]
                     / (config["k"] + rank))
            path = _path(seed, seed["term"], query, result, rank, score)
            existing = next((item for item in base if item["key"] == key), None)
            if existing is None:
                base.append({
                    "key": key, "paper": result, "base_score": 0.0,
                    "multihop_score": score, "first_round": False,
                    "from_multihop": True, "sources": [path],
                })
            else:
                existing["multihop_score"] += score
                existing["from_multihop"] = True
                existing["sources"].append(path)
    for item in base:
        item["score"] = item["base_score"] + item["multihop_score"]
        item["sources"].sort(key=lambda path: (path["seed_key"], path["node"]["type"],
                                               path["node"]["id"], path["rank"]))
    base.sort(key=lambda item: (-item["score"], item["key"]))
    failed = sum(request["status"] == "failed" for request in requests)
    if failed == 0:
        status = "ok"
    elif failed == len(requests):
        status = "failed"
    else:
        status = "partial"
    return {
        "query": query, "status": status,
        "trigger": {"applied": True, "reason": reason,
                    "top_score": top_score,
                    "min_seed_score": config["min_seed_score"]},
        "intent": intent,
        "seeds": seeds,
        "requests": requests,
        "results": base,
    }


def _number_field(value, context, minimum=0):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{context} must be a number")
    if not math.isfinite(float(value)) or value < minimum:
        raise ValueError(f"{context} must be finite and >= {minimum}")
    return float(value)


def _path_keys(path):
    return ("query", "term", "seed_key", "seed_title", "node", "paper_key",
            "rank", "score")


def validate_document(document):
    """Validate the independent ``multihop-v1`` output shape."""
    if not isinstance(document, dict):
        raise ValueError("Multi-hop output must be an object")
    if list(document) != ["schema_version", "rules_version", "source", "settings",
                          "summary", "searches"]:
        raise ValueError("Multi-hop output fields are not in the frozen order")
    if document["schema_version"] != MULTIHOP_SCHEMA_VERSION:
        raise ValueError("Multi-hop output schema_version is invalid")
    if document["rules_version"] != MULTIHOP_RULES_VERSION:
        raise ValueError("Multi-hop output rules_version is invalid")
    source = document["source"]
    if list(source) != ["results", "schema_version", "sha256"]:
        raise ValueError("Multi-hop source fields are invalid")
    if source["schema_version"] not in (2, 3):
        raise ValueError("Multi-hop source schema_version is invalid")
    validate_config(document["settings"])
    summary = document["summary"]
    if list(summary) != ["queries", "triggered_queries", "requests",
                         "failed_requests", "additional_papers"]:
        raise ValueError("Multi-hop summary fields are invalid")
    for key, value in summary.items():
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"Multi-hop summary.{key} must be an integer >= 0")
    searches = document["searches"]
    if summary["queries"] != len(searches):
        raise ValueError("Multi-hop summary.queries does not match searches")
    triggered = requests_total = failed_total = additional = 0
    for search in searches:
        if list(search) != ["query", "status", "trigger", "intent", "seeds",
                            "requests", "results"]:
            raise ValueError("Multi-hop search fields are invalid")
        if search["status"] not in ("ok", "partial", "failed", "skipped"):
            raise ValueError("Multi-hop search status is invalid")
        trigger = search["trigger"]
        if list(trigger) != ["applied", "reason", "top_score", "min_seed_score"]:
            raise ValueError("Multi-hop trigger fields are invalid")
        triggered += bool(trigger["applied"])
        _number_field(trigger["top_score"], "trigger.top_score")
        _number_field(trigger["min_seed_score"], "trigger.min_seed_score")
        if not isinstance(search["seeds"], list):
            raise ValueError("Multi-hop seeds must be a list")
        for seed in search["seeds"]:
            if list(seed) != ["type", "id", "name", "seed_key", "seed_rank",
                              "seed_title", "term", "weight"]:
                raise ValueError("Multi-hop seed fields are invalid")
            if seed["type"] not in ("author", "institution"):
                raise ValueError("Multi-hop seed type is invalid")
            _number_field(seed["weight"], "seed.weight")
        for request in search["requests"]:
            requests_total += 1
            failed_total += request.get("status") == "failed"
            if request.get("status") not in ("ok", "failed"):
                raise ValueError("Multi-hop request status is invalid")
        keys = []
        for result in search["results"]:
            if list(result) != ["key", "paper", "base_score", "multihop_score",
                                "first_round", "from_multihop", "sources", "score"]:
                raise ValueError("Multi-hop result fields are invalid")
            if not isinstance(result["paper"], dict):
                raise ValueError("Multi-hop result paper must be an object")
            if not isinstance(result["first_round"], bool) or not isinstance(
                    result["from_multihop"], bool):
                raise ValueError("Multi-hop result flags must be boolean")
            for key in ("base_score", "multihop_score", "score"):
                _number_field(result[key], f"result.{key}")
            if not math.isclose(result["score"],
                                result["base_score"] + result["multihop_score"],
                                rel_tol=0, abs_tol=1e-12):
                raise ValueError("Multi-hop result score is not base + hop")
            source_sum = sum(_number_field(path["score"], "path.score")
                             for path in result["sources"])
            if not math.isclose(result["multihop_score"], source_sum,
                                rel_tol=0, abs_tol=1e-12):
                raise ValueError("Multi-hop result multihop_score does not match paths")
            for path in result["sources"]:
                if list(path) != list(_path_keys(path)):
                    raise ValueError("Multi-hop path fields are invalid")
                if not isinstance(path["node"], dict):
                    raise ValueError("Multi-hop path node must be an object")
                if path["node"].get("type") not in ("author", "institution"):
                    raise ValueError("Multi-hop path node type is invalid")
            keys.append(result["key"])
        if len(keys) != len(set(keys)):
            raise ValueError("Multi-hop result keys must be unique")
        additional += sum(not result["first_round"] for result in search["results"])
        ordered = [(-result["score"], result["key"]) for result in search["results"]]
        if ordered != sorted(ordered):
            raise ValueError("Multi-hop results are not sorted")
    if summary["triggered_queries"] != triggered:
        raise ValueError("Multi-hop summary.triggered_queries does not match")
    if summary["requests"] != requests_total:
        raise ValueError("Multi-hop summary.requests does not match")
    if summary["failed_requests"] != failed_total:
        raise ValueError("Multi-hop summary.failed_requests does not match")
    if summary["additional_papers"] != additional:
        raise ValueError("Multi-hop summary.additional_papers does not match")
    return document


def run_multihop(args):
    """Run the separate multi-hop stage over a validated v2/v3 result file."""
    from . import verification

    path = Path(args.results)
    document = read_json(path)
    if not isinstance(document, dict):
        raise ValueError("Search output must be an object")
    version = document.get("schema_version")
    if version == 2:
        verification.validate_v2_document(document)
    elif version == 3:
        verification.validate_v3_document(document)
    else:
        raise ValueError("Multi-hop input must be schema version 2 or 3")
    config = load_config(args.config)
    client = AcemapClient(args.cache, args.timeout, args.retries,
                          args.cache_only, args.refresh)
    searches = [expand_query(row, client, config) for row in document["searches"]]
    additional = sum(sum(not item["first_round"] for item in search["results"])
                     for search in searches)
    output = {
        "schema_version": MULTIHOP_SCHEMA_VERSION,
        "rules_version": MULTIHOP_RULES_VERSION,
        "source": {"results": path.name, "schema_version": version,
                   "sha256": digest(path)},
        "settings": config,
        "summary": {
            "queries": len(searches),
            "triggered_queries": sum(search["trigger"]["applied"] for search in searches),
            "requests": sum(len(search["requests"]) for search in searches),
            "failed_requests": sum(
                request["status"] == "failed"
                for search in searches for request in search["requests"]),
            "additional_papers": additional,
        },
        "searches": searches,
    }
    validate_document(output)
    write_json(args.output, output)
    return 0 if all(search["status"] in ("ok", "skipped") for search in searches) else 1
