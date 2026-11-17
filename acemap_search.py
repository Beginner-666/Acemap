"""AI3602 task 3: local ConceptNet subgraph and Acemap query expansion.

Python 3.10+, standard library only. No network or processing at import time.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlencode
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parent
ENDPOINT = "https://acemap.info/api/v1/work/search"
USER_AGENT = "AI3602-PilotProject/1.0"


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def digest(path):
    result = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def normalize(text):
    return " ".join(str(text).casefold().replace("_", " ").split())


def concept(uri, language):
    parts = uri.split("/")
    if len(parts) < 4 or parts[1:3] != ["c", language]:
        return None
    # Ignore optional POS / sense suffixes when matching seed labels.
    return normalize(unquote(parts[3]))


def edge_order(edge):
    return (-edge["weight"], edge["assertion"], edge["subject_uri"], edge["object_uri"])


def build_graph(args):
    config = read_json(args.config)
    seeds = sorted(set(map(normalize, config["seeds"])))
    if not seeds or any(not seed for seed in seeds):
        raise ValueError("领域种子词不能为空")
    limit = int(config["edges_per_seed"])
    minimum = float(config["min_weight"])
    if limit < 1 or not math.isfinite(minimum) or minimum < 0:
        raise ValueError("edges_per_seed 必须为正数，min_weight 必须为非负有限数")
    buckets = {seed: {} for seed in seeds}
    allowed = set(config["relations"])
    opener = gzip.open if args.assertions.suffix == ".gz" else open
    skipped = 0
    rows = 0
    started = time.monotonic()
    csv.field_size_limit(16 * 1024 * 1024)
    with opener(args.assertions, "rt", encoding="utf-8", newline="") as stream:
        for line_number, row in enumerate(csv.reader(stream, delimiter="\t"), 1):
            rows += 1
            if rows % 1_000_000 == 0:
                print(f"Scanned {rows:,} rows in {time.monotonic() - started:.1f}s", file=sys.stderr, flush=True)
            if len(row) != 5:
                skipped += 1
                continue
            assertion, relation, subject_uri, object_uri, metadata_text = row
            if relation not in allowed:
                continue
            subject = concept(subject_uri, config["language"])
            obj = concept(object_uri, config["language"])
            if not subject or not obj or subject == obj:
                continue
            hits = sorted({subject, obj} & buckets.keys())
            if not hits:
                continue
            try:
                metadata = json.loads(metadata_text)
                weight = float(metadata["weight"])
                if not math.isfinite(weight):
                    raise ValueError("nonfinite weight")
            except (ValueError, TypeError, KeyError):
                skipped += 1
                continue
            if weight < minimum:
                continue
            edge = {
                "assertion": assertion, "relation": relation,
                "subject": subject, "object": obj,
                "subject_uri": subject_uri, "object_uri": object_uri,
                "weight": weight, "source_line": line_number,
                "metadata": metadata,
            }
            for seed in hits:
                buckets[seed][assertion] = edge
                if len(buckets[seed]) > 2 * limit:
                    best = sorted(buckets[seed].values(), key=edge_order)[:limit]
                    buckets[seed] = {item["assertion"]: item for item in best}
    selected = {}
    for bucket in buckets.values():
        for edge in sorted(bucket.values(), key=edge_order)[:limit]:
            selected[edge["assertion"]] = edge
    if not selected:
        raise ValueError("没有抽取到图谱边；请检查数据格式、种子词和过滤条件")
    edges = sorted(selected.values(), key=edge_order)
    nodes = sorted({edge[key] for edge in edges for key in ("subject", "object")})
    write_json(args.output, {
        "schema_version": 1, "domain": config["domain"],
        "extraction_config": config,
        "source": {"name": config["source_name"], "url": config["source_url"],
                   "file_name": args.assertions.name, "sha256": digest(args.assertions)},
        "statistics": {"rows_read": rows, "malformed_rows": skipped,
                       "nodes": len(nodes), "edges": len(edges)},
        "nodes": nodes, "edges": edges,
    })
    print(f"已保存图谱：{args.output}（{len(nodes)} 个节点，{len(edges)} 条边）")


def expand(graph, query, limit):
    query = normalize(query)
    seeds = set(map(normalize, graph.get("extraction_config", {}).get("seeds", [])))
    support = {}
    for edge in graph["edges"]:
        for source, target in ((edge["subject"], edge["object"]), (edge["object"], edge["subject"])):
            if source in seeds:
                support.setdefault(target, set()).add(source)
    candidates = {}
    for edge in graph["edges"]:
        # Traversal is bidirectional for related-term retrieval only.
        # The stored subject/relation/object direction is preserved as evidence.
        if query == edge["subject"]:
            neighbor = edge["object"]
        elif query == edge["object"]:
            neighbor = edge["subject"]
        else:
            continue
        existing = candidates.get(neighbor)
        if existing is None or edge_order(edge) < edge_order(existing):
            candidates[neighbor] = edge
    # Prefer neighbors also linked to another domain seed, reducing polysemy drift.
    # For sparse concepts with no extra support, retain the original edge ranking.
    def candidate_order(pair):
        term, edge = pair
        anchors = support.get(term, set()) - {query}
        return (not bool(anchors), edge_order(edge), term)
    ordered = sorted(candidates.items(), key=candidate_order)
    return [{"term": term, "evidence": edge,
             "domain_support_seeds": sorted(support.get(term, set()) - {query})}
            for term, edge in ordered[:limit]]


def extract_records(payload):
    """Accept explicit common envelopes; never turn unknown JSON into no results."""
    if isinstance(payload, dict):
        if payload.get("success") is False:
            raise ValueError("Acemap 返回 success=false")
        if payload.get("error"):
            raise ValueError("Acemap 返回错误对象，请查看原始响应")
    def locate(value):
        if isinstance(value, list):
            return value
        if not isinstance(value, dict):
            return None
        if isinstance(value.get("hits"), dict):
            return locate(value["hits"])
        for key in ("data", "results", "items", "records", "list", "content", "works", "hits"):
            if key in value:
                found = locate(value[key])
                if found is not None:
                    return found
        return None
    records = locate(payload)
    if records is None:
        raise ValueError("尚不支持该接口响应结构；原始响应已缓存，需根据真实字段适配")
    normalized = []
    for item in records:
        if not isinstance(item, dict):
            raise ValueError("论文列表中包含非对象记录")
        record = item.get("_source", item)
        if not isinstance(record, dict):
            raise ValueError("论文 _source 不是对象")
        record = dict(record)
        if "_id" in item and "id" not in record:
            record["id"] = item["_id"]
        normalized.append(record)
    return normalized


class AcemapClient:
    def __init__(self, cache_dir, timeout, retries, cache_only, refresh):
        self.cache_dir = cache_dir
        self.timeout = timeout
        self.retries = retries
        self.cache_only = cache_only
        self.refresh = refresh

    def search(self, keyword, size):
        params = {"keyword": keyword, "order": "desc", "page": 1, "size": size}
        url = ENDPOINT + "?" + urlencode(params)
        key = hashlib.sha256(url.encode("utf-8")).hexdigest()
        path = self.cache_dir / f"{key}.json"
        if path.exists() and not self.refresh:
            snapshot = read_json(path)
            if snapshot.get("url") != url:
                raise ValueError("缓存请求信息不匹配")
        else:
            if self.cache_only:
                raise ValueError(f"缺少离线响应缓存：{path}")
            raw = self.fetch(url)
            snapshot = {"url": url, "parameters": params,
                        "retrieved_at": datetime.now(timezone.utc).isoformat(),
                        "raw_response": raw}
            # Save the original body before decoding or adapting its schema.
            write_json(path, snapshot)
        payload = json.loads(snapshot["raw_response"])
        return extract_records(payload), path.name

    def fetch(self, url):
        for attempt in range(self.retries + 1):
            try:
                request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
                with urlopen(request, timeout=self.timeout) as response:
                    return response.read().decode("utf-8-sig")
            except HTTPError as exc:
                if exc.code not in (429, 500, 502, 503, 504) or attempt == self.retries:
                    raise ValueError(f"Acemap HTTP {exc.code}：{url}") from exc
            except (URLError, TimeoutError, OSError) as exc:
                if attempt == self.retries:
                    raise ValueError(f"Acemap 网络请求失败：{exc}") from exc
            time.sleep(min(2 ** attempt, 8))
        raise ValueError("Acemap 请求未完成")


def identity(record):
    doi = record.get("doi")
    if doi:
        doi = re.sub(r"^(https?://(dx\.)?doi\.org/|doi:\s*)", "", str(doi), flags=re.I)
        return "doi:" + normalize(doi)
    for key in ("id", "work_id", "paper_id", "paperId", "workId"):
        if record.get(key) is not None:
            return key + ":" + str(record[key])
    title = record.get("title") or record.get("display_name") or record.get("name")
    if isinstance(title, str) and title.strip():
        return "title:" + normalize(title)
    # Unknown fields are preserved, but only identical records are merged.
    return "record:" + hashlib.sha256(json.dumps(record, sort_keys=True).encode()).hexdigest()


def search_one(args, graph, client, query, expansion=None):
    if expansion is None:
        expansion = expand(graph, query, args.max_expansions)
    terms = [query] + [item["term"] for item in expansion]
    merged = {}
    requests = []
    baseline_ids = set()
    for term_index, term in enumerate(terms):
        try:
            records, cache_name = client.search(term, args.size)
        except (ValueError, OSError) as exc:
            requests.append({"term": term, "status": "failed", "error": str(exc)})
            continue
        requests.append({"term": term, "status": "ok", "count": len(records), "cache_file": cache_name})
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
    if getattr(args, "knowledge", "graph") != "graph":
        from conceptnet_dynamic import run_dynamic_search
        return run_dynamic_search(args)
    if args.graph is None:
        raise ValueError("graph 模式需要通过 --graph 显式提供图谱文件")
    graph = read_json(args.graph)
    if graph.get("schema_version") != 1 or not graph.get("edges"):
        raise ValueError("图谱文件版本不支持或没有图谱边")
    queries = ([args.query] if args.query is not None else
               [line.strip() for line in args.queries.read_text(encoding="utf-8-sig").splitlines()
                if line.strip() and not line.lstrip().startswith("#")])
    queries = list(dict.fromkeys(normalize(query) for query in queries))
    if not queries or any(not query for query in queries):
        raise ValueError("查询不能为空")
    client = AcemapClient(args.cache, args.timeout, args.retries, args.cache_only, args.refresh)
    results = [search_one(args, graph, client, query) for query in queries]
    successful = sum(result["status"] == "ok" for result in results)
    write_json(args.output, {
        "schema_version": 1, "domain": graph["domain"], "endpoint": ENDPOINT,
        "graph_sha256": digest(args.graph), "source": graph["source"],
        "settings": {"size_per_term": args.size, "max_expansions": args.max_expansions,
                     "expansion_ranking": "other-domain-seed support first, then edge weight and URI",
                     "fusion": "weighted RRF: original=1, expansion=0.5, k=60",
                     "page": 1, "order": "desc"},
        "summary": {"queries": len(results), "successful_queries": successful,
                    "successful_with_enhancement": sum(
                        row["status"] == "ok" and row["enhancement_applied"] for row in results)},
        "searches": results,
    })
    print(f"结果已保存：{args.output}；完整成功 {successful}/{len(results)}")
    return 0 if successful == len(results) else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build-graph", help="从本地 ConceptNet assertions TSV/TSV.GZ 抽取领域子图")
    build.add_argument("--assertions", type=Path, required=True)
    build.add_argument("--config", type=Path, required=True)
    build.add_argument("--output", type=Path, required=True)
    index = commands.add_parser("build-index", help="一次性构建跨领域 ConceptNet 英文索引")
    index.add_argument("--assertions", type=Path, required=True)
    index.add_argument("--output", type=Path, default=ROOT / "data/conceptnet_en.sqlite")
    search = commands.add_parser("search", help="基于图谱扩展关键词并查询 Acemap")
    selection = search.add_mutually_exclusive_group(required=True)
    selection.add_argument("--query")
    selection.add_argument("--queries", type=Path)
    search.add_argument("--graph", type=Path, help="graph 模式下显式指定的图谱文件")
    search.add_argument("--knowledge", choices=("auto", "local", "online", "graph"), default="auto",
                        help="auto: 在线 ConceptNet，不可用时回退本地通用索引；graph: 旧领域子图")
    search.add_argument("--index", type=Path, default=ROOT / "data/conceptnet_en.sqlite")
    search.add_argument("--conceptnet-cache", type=Path, default=ROOT / "data/conceptnet_cache")
    search.add_argument("--cache", type=Path, default=ROOT / "data/acemap_cache")
    search.add_argument("--output", type=Path, default=ROOT / "results/search_results.json")
    search.add_argument("--max-expansions", type=int, default=3)
    search.add_argument("--size", type=int, default=10)
    search.add_argument("--timeout", type=float, default=20)
    search.add_argument("--retries", type=int, default=2)
    mode = search.add_mutually_exclusive_group()
    mode.add_argument("--cache-only", action="store_true", help="只读取已有响应，禁止联网")
    mode.add_argument("--refresh", action="store_true", help="重新请求并覆盖相同请求的缓存")
    args = parser.parse_args()
    if args.command == "search":
        if (args.max_expansions < 1 or not 1 <= args.size <= 100 or args.retries < 0
                or not math.isfinite(args.timeout) or args.timeout <= 0):
            parser.error("扩展数至少为 1，size 为 1–100，重试次数非负，timeout 为正有限数")
    try:
        if args.command == "build-graph":
            build_graph(args)
            return 0
        if args.command == "build-index":
            from conceptnet_dynamic import build_index
            build_index(args.assertions, args.output)
            return 0
        return run_search(args)
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as exc:
        print(f"操作失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
