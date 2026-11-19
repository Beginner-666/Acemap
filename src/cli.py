"""Command-line entry point for the dynamic ConceptNet pipeline."""

from __future__ import annotations

import argparse
import math
from pathlib import Path
import sqlite3
import sys

from . import constants
from .conceptnet_index import build_index
from .search import run_search


def build_parser():
    parser = argparse.ArgumentParser(
        description="AI3602 task 3: dynamic ConceptNet query expansion for Acemap.")
    commands = parser.add_subparsers(dest="command", required=True)

    index = commands.add_parser("build-index", help="一次性构建跨领域 ConceptNet 英文索引")
    index.add_argument("--assertions", type=Path, required=True)
    index.add_argument("--output", type=Path, default=constants.DEFAULT_INDEX)

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
    mode = search.add_mutually_exclusive_group()
    mode.add_argument("--cache-only", action="store_true", help="只读取已有响应，禁止联网")
    mode.add_argument("--refresh", action="store_true", help="重新请求并覆盖相同请求的缓存")
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "search":
        if (args.max_expansions < 1 or not 1 <= args.size <= 100 or args.retries < 0
                or not math.isfinite(args.timeout) or args.timeout <= 0):
            parser.error("扩展数至少为 1，size 为 1–100，重试次数非负，timeout 为正有限数")
    try:
        if args.command == "build-index":
            build_index(args.assertions, args.output)
            return 0
        return run_search(args)
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as exc:
        print(f"操作失败：{exc}", file=sys.stderr)
        return 1
