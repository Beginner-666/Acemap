"""Command-line entry point for the dynamic ConceptNet pipeline."""

from __future__ import annotations

import argparse
import math
from pathlib import Path
import sqlite3
import sys

from . import constants
from .conceptnet_index import build_index
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
        return run_search(args)
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as exc:
        print(f"操作失败：{exc}", file=sys.stderr)
        return 1
