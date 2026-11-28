"""Idempotent ConceptNet snapshot cache-key migration.

The migration is deliberately split into plan/apply/rollback so that the CLI
can dry-run, test code can inspect actions, and a rerun is a no-op.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from . import constants, graph_index, jsonio
from .versions import CONCEPTNET_ONLINE_SOURCE_ID, SNAPSHOT_KEY_SCHEMA

MIGRATION_NAME = "conceptnet-cache-key-v2"
MANIFEST_SCHEMA = 1


def _old_key(mode, term):
    return hashlib.sha256((mode + "\0" + term).encode()).hexdigest()


def _new_key(mode, source_id, term):
    return hashlib.sha256(
        f"v{SNAPSHOT_KEY_SCHEMA}\0{mode}\0{source_id}\0{term}".encode()).hexdigest()


def infer_mode(path, snapshot):
    """Identify the knowledge mode from the legacy filename."""
    term = snapshot.get("query")
    if not isinstance(term, str) or not term:
        return None
    for mode in ("auto", "local", "online"):
        if _old_key(mode, term) == path.stem:
            return mode
    return None


def source_info(snapshot):
    """Return ``(source_id, graph_id)`` for a legacy snapshot.

    Online snapshots have no local graph id; their source id is the frozen
    online identifier.
    """
    backend = snapshot.get("backend")
    if backend == "online":
        return CONCEPTNET_ONLINE_SOURCE_ID, None
    if backend == "local":
        source = snapshot.get("source")
        if not isinstance(source, dict):
            raise ValueError("Local snapshot is missing its manifest")
        graph_id = graph_index.derive_graph_id(source)
        return "local:" + graph_id, graph_id
    raise ValueError(f"Snapshot backend has no cache source: {backend}")


def source_id_for(snapshot):
    return source_info(snapshot)[0]


def plan(cache_dir):
    """Describe the rename actions without changing the filesystem."""
    cache_dir = Path(cache_dir)
    actions = []
    for path in sorted(cache_dir.glob("*.json")):
        action = {"old": path.name, "new": None, "mode": None, "source_id": None,
                  "graph_id": None, "query": None, "backend": None,
                  "sha256": None, "status": "skipped", "reason": None}
        try:
            action["sha256"] = jsonio.digest(path)
            snapshot = jsonio.read_json(path)
        except (OSError, ValueError) as exc:
            action["reason"] = f"invalid snapshot: {exc}"
            actions.append(action)
            continue
        if not isinstance(snapshot, dict):
            action["reason"] = "snapshot is not an object"
            actions.append(action)
            continue
        action["query"] = snapshot.get("query")
        action["backend"] = snapshot.get("backend")
        mode = infer_mode(path, snapshot)
        if mode is None:
            action["reason"] = "filename does not match a legacy key"
            actions.append(action)
            continue
        action["mode"] = mode
        try:
            source_id, graph_id = source_info(snapshot)
        except ValueError as exc:
            action["reason"] = str(exc)
            actions.append(action)
            continue
        action["source_id"] = source_id
        action["graph_id"] = graph_id
        action["new"] = _new_key(mode, source_id, snapshot["query"]) + ".json"
        target = cache_dir / action["new"]
        if target.exists():
            if target.read_bytes() == path.read_bytes():
                action["status"] = "duplicate"
            else:
                action["status"] = "conflict"
        else:
            action["status"] = "planned"
        actions.append(action)
    return actions


def apply(cache_dir, actions=None):
    """Apply planned renames; conflicts abort before any rename."""
    cache_dir = Path(cache_dir)
    actions = list(actions if actions is not None else plan(cache_dir))
    conflicts = [action for action in actions if action["status"] == "conflict"]
    if conflicts:
        names = ", ".join(action["old"] for action in conflicts)
        raise ValueError(f"Cache migration conflicts: {names}")
    applied = []
    for action in actions:
        result = dict(action)
        if action["status"] != "planned":
            applied.append(result)
            continue
        old = cache_dir / action["old"]
        new = cache_dir / action["new"]
        if not old.exists():
            result["status"] = "missing"
        elif new.exists():
            if new.read_bytes() != old.read_bytes():
                raise ValueError(f"Cache migration conflict: {action['new']}")
            result["status"] = "duplicate"
        else:
            old.rename(new)
            result["status"] = "renamed"
        applied.append(result)
    return applied


def _display_path(path):
    path = Path(path)
    try:
        return str(path.resolve().relative_to(constants.ROOT.resolve()))
    except ValueError:
        return str(path)


def manifest(cache_dir, actions):
    return {
        "schema": MANIFEST_SCHEMA,
        "migration": MIGRATION_NAME,
        "cache_key_schema": SNAPSHOT_KEY_SCHEMA,
        "cache_dir": _display_path(cache_dir),
        "actions": actions,
    }


def rollback(manifest_path, cache_dir=None):
    """Restore renamed files from a migration manifest."""
    document = jsonio.read_json(manifest_path)
    if document.get("migration") != MIGRATION_NAME:
        raise ValueError("Not a ConceptNet cache migration manifest")
    cache_dir = Path(cache_dir or document["cache_dir"])
    if not cache_dir.is_absolute():
        cache_dir = constants.ROOT / cache_dir
    restored = []
    for action in document.get("actions", []):
        if action.get("status") != "renamed":
            continue
        old = cache_dir / action["old"]
        new = cache_dir / action["new"]
        if new.exists() and not old.exists():
            new.rename(old)
            restored.append(action["old"])
    return restored
