#!/usr/bin/env python3
"""M0 migrations: ConceptNet snapshot cache keys and graph_id metadata.

Examples:
    python scripts/m0_migrate.py cache-key --dry-run
    python scripts/m0_migrate.py cache-key --apply
    python scripts/m0_migrate.py graph-id --apply
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src import cache_migration, constants, graph_index, jsonio  # noqa: E402

DEFAULT_MIGRATIONS = ROOT / "results" / "migrations"


def _print(value):
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=False))


def _display_path(path):
    path = Path(path)
    try:
        return str(path.resolve().relative_to(ROOT.resolve()))
    except ValueError:
        return str(path)


def cache_key(args):
    if args.rollback:
        restored = cache_migration.rollback(args.manifest, args.cache_dir)
        _print({"migration": cache_migration.MIGRATION_NAME,
                "rollback": restored})
        return 0
    actions = cache_migration.plan(args.cache_dir)
    if not args.apply:
        _print({"migration": cache_migration.MIGRATION_NAME,
                "dry_run": True, "actions": actions})
        return 0
    if not any(action["status"] == "planned" for action in actions):
        _print({"migration": cache_migration.MIGRATION_NAME,
                "dry_run": False, "noop": True, "manifest": str(args.manifest)})
        return 0
    actions = cache_migration.apply(args.cache_dir, actions)
    # Preserve an earlier manifest so a second apply cannot erase rollback data.
    previous = []
    if args.manifest.exists():
        try:
            existing = jsonio.read_json(args.manifest)
            if existing.get("migration") == cache_migration.MIGRATION_NAME:
                previous = [item for item in existing.get("actions", [])
                            if isinstance(item, dict) and item.get("old")]
        except (OSError, ValueError):
            previous = []
    merged = {item["old"]: item for item in previous}
    for action in actions:
        if action["status"] in ("renamed", "duplicate"):
            merged[action["old"]] = action
    document = cache_migration.manifest(
        args.cache_dir, [merged[key] for key in sorted(merged)])
    jsonio.write_json(args.manifest, document)
    _print({"migration": cache_migration.MIGRATION_NAME,
            "dry_run": False, "manifest": str(args.manifest),
            "renamed": sum(action["status"] == "renamed" for action in actions),
            "duplicates": sum(action["status"] == "duplicate" for action in actions),
            "skipped": sum(action["status"] == "skipped" for action in actions)})
    return 0


def graph_id(args):
    result = graph_index.ensure_graph_id(args.index, apply=args.apply)
    if args.apply and result["applied"]:
        jsonio.write_json(args.manifest, {
            "schema": 1,
            "migration": "graph-id-v1",
            "index": _display_path(args.index),
            "graph_id": result["graph_id"],
            "existing": result["existing"],
            "applied": result["applied"],
        })
    _print({"migration": "graph-id-v1", "dry_run": not args.apply,
            "index": _display_path(args.index), **result})
    return 0


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    cache = commands.add_parser("cache-key", help="Migrate ConceptNet snapshot keys")
    cache.add_argument("--cache-dir", type=Path, default=constants.DEFAULT_CONCEPTNET_CACHE)
    cache.add_argument("--manifest", type=Path,
                       default=DEFAULT_MIGRATIONS / "conceptnet_cache_v2.json")
    cache.add_argument("--apply", action="store_true")
    cache.add_argument("--dry-run", action="store_true")
    cache.add_argument("--rollback", action="store_true")
    cache.set_defaults(func=cache_key)

    graph = commands.add_parser("graph-id", help="Add or verify index graph_id metadata")
    graph.add_argument("--index", type=Path, default=constants.DEFAULT_INDEX)
    graph.add_argument("--manifest", type=Path,
                       default=DEFAULT_MIGRATIONS / "graph_id.json")
    graph.add_argument("--apply", action="store_true")
    graph.add_argument("--dry-run", action="store_true")
    graph.set_defaults(func=graph_id)
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "apply", False) and getattr(args, "dry_run", False):
        parser.error("--apply and --dry-run are mutually exclusive")
    return args.func(args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, KeyError) as error:
        print(f"Migration failed: {error}", file=sys.stderr)
        sys.exit(1)
