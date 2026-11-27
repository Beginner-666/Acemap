"""Command-line entry point for the dynamic ConceptNet pipeline."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import sqlite3
import sys

from . import constants
from .conceptnet_index import build_index
from .jsonio import digest, read_json, write_json
from .rankers import RANKER_VERSIONS
from .search import run_search


def build_parser():
    parser = argparse.ArgumentParser(
        description="AI3602 task 3: dynamic ConceptNet query expansion for Acemap.")
    commands = parser.add_subparsers(dest="command", required=True)

    index = commands.add_parser("build-index", help="一次性构建跨领域 ConceptNet 英文索引")
    index.add_argument("--assertions", type=Path, required=True)
    index.add_argument("--output", type=Path, default=constants.DEFAULT_INDEX)

    clean = commands.add_parser(
        "clean-graph",
        help="从原始 ConceptNet 索引/断言构建可选的 clean-v1 图谱与质量报告")
    clean.add_argument("--input", type=Path, required=True)
    clean.add_argument("--output", type=Path,
                       default=constants.ROOT / "data" / "conceptnet_clean.sqlite")
    clean.add_argument("--min-weight", type=float, default=1.0)
    clean.add_argument("--degree-cap", type=int, default=500)
    clean.add_argument("--domain-terms", type=Path,
                       default=constants.ROOT / "config" / "domain_terms.txt")
    clean.add_argument("--extensions", type=Path, action="append", default=[],
                       help="可重复；JSON/JSONL 扩展边输入")
    clean.add_argument("--merge-extensions", action="store_true",
                       help="显式把扩展边合并进 edges；默认只写独立 extensions 表")
    clean.add_argument("--quality-report", type=Path,
                       default=constants.ROOT / "results" / "graph_quality.json")
    clean.add_argument("--sample-size", type=int, default=20)

    search = commands.add_parser("search", help="基于动态 ConceptNet 邻居扩展关键词并查询 Acemap")
    selection = search.add_mutually_exclusive_group(required=True)
    selection.add_argument("--query")
    selection.add_argument("--queries", type=Path)
    search.add_argument("--knowledge", choices=("auto", "local", "online"), default="auto",
                        help="auto: 在线 ConceptNet，不可用时回退本地索引；"
                             "local: 仅本地索引；online: 仅在线服务")
    search.add_argument("--index", type=Path, default=constants.DEFAULT_INDEX)
    search.add_argument("--conceptnet-cache", type=Path,
                        default=constants.DEFAULT_CONCEPTNET_CACHE)
    search.add_argument("--cache", type=Path, default=constants.DEFAULT_ACEAMP_CACHE)
    search.add_argument("--output", type=Path, default=constants.DEFAULT_OUTPUT)
    search.add_argument("--max-expansions", type=int, default=3)
    search.add_argument("--size", type=int, default=10)
    search.add_argument("--timeout", type=float, default=20)
    search.add_argument("--retries", type=int, default=2)
    search.add_argument("--schema-version", type=int, choices=(2, 3), default=2,
                        help="2: 冻结的原有输出；3: 可选的重排输出")
    search.add_argument("--ranker", choices=tuple(RANKER_VERSIONS),
                        help="schema 3 使用的排序器；省略时等价于 rrf")
    search.add_argument("--ranking-config", type=Path,
                        default=constants.ROOT / "config" / "ranking.json")
    mode = search.add_mutually_exclusive_group()
    mode.add_argument("--cache-only", action="store_true", help="只读取已有响应，禁止联网")
    mode.add_argument("--refresh", action="store_true", help="重新请求并覆盖相同请求的缓存")

    multihop = commands.add_parser(
        "multihop", help="对首轮搜索结果做可关闭的一跳作者/机构扩展")
    multihop.add_argument("--results", type=Path,
                          default=constants.ROOT / "results" / "dynamic_queries_v3.json")
    multihop.add_argument("--output", type=Path,
                          default=constants.ROOT / "results" / "dynamic_queries_multihop.json")
    multihop.add_argument("--config", type=Path,
                          default=constants.ROOT / "config" / "multihop.json")
    multihop.add_argument("--cache", type=Path, default=constants.DEFAULT_ACEAMP_CACHE)
    multihop.add_argument("--timeout", type=float, default=20)
    multihop.add_argument("--retries", type=int, default=2)
    multihop.add_argument("--cache-only", action="store_true", help="只读取已有响应，禁止联网")
    multihop.add_argument("--refresh", action="store_true", help="重新请求并覆盖相同请求的缓存")
    multihop.set_defaults(command="multihop")

    evaluate = commands.add_parser(
        "evaluate", help="用固定查询集与人工 qrels 比较 baseline/rrf/graph_rerank")
    evaluate.add_argument("--queries", type=Path,
                          default=constants.ROOT / "config" / "evaluation_queries.json")
    evaluate.add_argument("--qrels", type=Path,
                          default=constants.ROOT / "config" / "qrels.json")
    evaluate.add_argument("--config", type=Path,
                          default=constants.ROOT / "config" / "evaluation.json")
    evaluate.add_argument("--results", action="append", metavar="NAME=PATH",
                          help="可重复；默认 baseline/eval_baseline.json、rrf/eval_rrf.json、graph_rerank/eval_graph_rerank.json")
    evaluate.add_argument("--qrels-provenance",
                          default="ai-assisted rubric; human review pending",
                          help="写入报告的标注来源说明，例如 human 或 ai-assisted")
    evaluate.add_argument("--output", type=Path,
                          default=constants.ROOT / "results" / "evaluation_report.json")
    evaluate.set_defaults(command="evaluate")

    graph = commands.add_parser(
        "graph-analyze", help="离线运行可复现的图算法并输出候选解释")
    graph.add_argument("--edges", type=Path, required=True,
                       help="JSON 边文件，或包含 edges 数组的快照")
    graph.add_argument("--seeds", nargs="+", required=True)
    graph.add_argument("--algorithm", choices=("pagerank", "weighted_path",
                                                 "common_neighbors", "adamic_adar",
                                                 "community", "meta_path"),
                       default="pagerank")
    graph.add_argument("--top-k", type=int, default=10)
    graph.add_argument("--max-hops", type=int, default=3)
    graph.add_argument("--iterations", type=int, default=50)
    graph.add_argument("--damping", type=float, default=0.85)
    graph.add_argument("--relations", nargs="*", default=[],
                       help="meta_path 的关系序列，例如 /r/IsA /r/RelatedTo")
    graph.add_argument("--output", type=Path,
                       default=constants.ROOT / "results" / "graph_analysis.json")

    rag = commands.add_parser(
        "rag", help="基于搜索结果和图谱证据生成可引用的 RAG 回答")
    rag.add_argument("--query", required=True)
    rag.add_argument("--results", type=Path,
                     help="可选的 v2/v3 搜索结果文件；省略时输出证据不足回答")
    rag.add_argument("--max-facts", type=int, default=12)
    rag.add_argument("--max-papers", type=int, default=8)
    rag.add_argument("--output", type=Path,
                     default=constants.ROOT / "results" / "rag.json")

    profile = commands.add_parser(
        "profile", help="根据显式授权的历史记录构建用户兴趣画像")
    profile.add_argument("--history", type=Path, required=True)
    profile.add_argument("--consent", action="store_true",
                         help="明确允许使用该历史记录")
    profile.add_argument("--reference-year", type=int, default=2025)
    profile.add_argument("--half-life-years", type=float, default=5.0)
    profile.add_argument("--max-events", type=int, default=100)
    profile.add_argument("--output", type=Path,
                         default=constants.ROOT / "results" / "profile.json")

    personalize = commands.add_parser(
        "personalize", help="将显式授权的兴趣画像应用到已有搜索结果")
    personalize.add_argument("--results", type=Path, required=True)
    personalize.add_argument("--history", type=Path)
    personalize.add_argument("--consent", action="store_true",
                             help="明确允许使用该历史记录")
    personalize.add_argument("--alpha", type=float, default=0.2)
    personalize.add_argument("--reference-year", type=int, default=2025)
    personalize.add_argument("--half-life-years", type=float, default=5.0)
    personalize.add_argument("--max-events", type=int, default=100)
    personalize.add_argument("--output", type=Path,
                             default=constants.ROOT / "results" / "personalized.json")

    api = commands.add_parser(
        "check-api", help="校验可选 API 配置；不会发起网络请求或显示密钥")
    api.add_argument("--config", type=Path,
                     default=constants.ROOT / "config" / "api.example.json")
    api.add_argument("--output", type=Path,
                     help="可选；将安全的 provider 状态写入 JSON")
    serve = commands.add_parser("serve", help="Start the visualization and API server")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "search":
        if (args.max_expansions < 1 or not 1 <= args.size <= 100 or args.retries < 0
                or not math.isfinite(args.timeout) or args.timeout <= 0):
            parser.error("扩展数至少为 1，size 为 1–100，重试次数非负，timeout 为正有限数")
        if args.schema_version == 2 and args.ranker is not None:
            parser.error("--ranker 仅用于 --schema-version 3；schema 2 输出保持冻结")
    elif args.command == "clean-graph":
        if (not math.isfinite(args.min_weight) or args.min_weight < 0
                or args.degree_cap < 1 or args.sample_size < 0):
            parser.error("min-weight 需为非负有限数，degree-cap 至少为 1，sample-size 非负")
        if args.merge_extensions and not args.extensions:
            parser.error("--merge-extensions 需要至少一个 --extensions 输入")
    elif args.command == "multihop":
        if (args.retries < 0 or not math.isfinite(args.timeout) or args.timeout <= 0):
            parser.error("重试次数非负，timeout 为正有限数")
    elif args.command == "graph-analyze":
        if args.top_k < 1 or args.max_hops < 1 or args.iterations < 1 \
                or not 0 <= args.damping < 1:
            parser.error("top-k、max-hops、iterations 必须为正数，damping 必须在 [0,1) 内")
    elif args.command == "rag":
        if args.max_facts < 0 or args.max_papers < 0:
            parser.error("RAG 证据数量不能为负数")
    elif args.command in ("profile", "personalize"):
        if (args.max_events < 1 or args.half_life_years <= 0
                or not math.isfinite(args.half_life_years)):
            parser.error("max-events 至少为 1，half-life-years 必须为正有限数")
        if args.command == "personalize" and not 0 <= args.alpha <= 1:
            parser.error("alpha 必须在 [0,1] 内")
    try:
        if args.command == "build-index":
            build_index(args.assertions, args.output)
            return 0
        if args.command == "clean-graph":
            from .graph_clean import clean_graph
            clean_graph(args.input, args.output, min_weight=args.min_weight,
                        degree_cap=args.degree_cap,
                        domain_terms=args.domain_terms,
                        extensions=args.extensions,
                        merge_extensions=args.merge_extensions,
                        quality_report=args.quality_report,
                        sample_size=args.sample_size, verbose=True)
            return 0
        if args.command == "multihop":
            from .multihop import run_multihop
            return run_multihop(args)
        if args.command == "evaluate":
            from .evaluation import run_evaluation
            return run_evaluation(args)
        if args.command == "graph-analyze":
            from .graph_algorithms import analyze_graph
            value = read_json(args.edges)
            edges = value.get("edges", []) if isinstance(value, dict) else value
            if args.algorithm == "pagerank":
                parameters = {"iterations": args.iterations, "damping": args.damping}
            elif args.algorithm == "weighted_path":
                parameters = {"max_hops": args.max_hops}
            else:
                parameters = {}
            if args.algorithm == "meta_path":
                parameters["relation_sequence"] = args.relations
            output = analyze_graph(
                edges, args.seeds, args.algorithm, args.top_k,
                source={"file": args.edges.name, "sha256": digest(args.edges)},
                **parameters)
            write_json(args.output, output)
            return 0
        if args.command == "rag":
            from .rag import run_rag
            return run_rag(args)
        if args.command == "profile":
            from .profile import run_profile
            return run_profile(args)
        if args.command == "personalize":
            from .profile import run_personalize
            return run_personalize(args)
        if args.command == "check-api":
            from .api_clients import load_api_config, provider_status
            config = load_api_config(args.config)
            output = {"schema_version": "api-status-v1",
                      "config": args.config.name,
                      "providers": provider_status(config)}
            if args.output:
                write_json(args.output, output)
            else:
                print(json.dumps(output, ensure_ascii=False, indent=2))
            return 0
        if args.command == "serve":
            from .server import serve
            serve(args.host, args.port)
            return 0
        return run_search(args)
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as exc:
        print(f"操作失败：{exc}", file=sys.stderr)
        return 1
