"""Graph index reader, identifiers, and validation shared by client code.

The provider and the M2 cleaning work must agree on this contract.  See
``docs/interface_protocol.md`` §6–§7.
"""

from __future__ import annotations

import json
import math
import sqlite3
from contextlib import closing
from pathlib import Path

from .versions import (GRAPH_RULES_VERSION, INDEX_SCHEMA_VERSION,
                       canonical_json, sha256_text)

EDGE_COLUMNS = ("assertion", "subject", "object", "weight", "evidence")
MANIFEST_KEYS = ("schema_version", "source_name", "source_url", "source_sha256",
                 "source_file", "rows_scanned", "edges_indexed", "language",
                 "relations", "min_weight")


def _graph_payload(manifest, rules_version):
    try:
        return {
            "source_name": manifest["source_name"],
            "source_sha256": manifest["source_sha256"],
            "language": manifest["language"],
            "relations": sorted(manifest["relations"]),
            "min_weight": manifest["min_weight"],
            "rules_version": rules_version,
        }
    except (KeyError, TypeError) as exc:
        raise ValueError(f"Graph manifest is missing a graph_id field: {exc}") from exc


def derive_graph_id(manifest, rules_version=GRAPH_RULES_VERSION):
    """Derive a stable graph identifier from manifest content and rules."""
    payload = _graph_payload(manifest, rules_version)
    return "g-" + sha256_text(canonical_json(payload))[:16]


def read_manifest(db):
    """Read and minimally validate the index manifest."""
    row = db.execute("SELECT value FROM metadata WHERE key='manifest'").fetchone()
    if row is None:
        raise ValueError("ConceptNet index manifest is missing")
    try:
        manifest = json.loads(row[0])
    except (TypeError, ValueError) as exc:
        raise ValueError("ConceptNet index manifest is not valid JSON") from exc
    if not isinstance(manifest, dict):
        raise ValueError("ConceptNet index manifest is not an object")
    return manifest


def read_graph_id(db):
    """Return the stored graph_id, or derive it for pre-M0 indexes."""
    row = db.execute("SELECT value FROM metadata WHERE key='graph_id'").fetchone()
    if row is not None and row[0]:
        return str(row[0])
    return derive_graph_id(read_manifest(db))


def edges_for_term(db, term, limit=1000):
    """Return the same evidence set the provider has always used.

    Both indexed endpoints are queried separately to avoid an OR scan;
    duplicate assertions keep the last parsed evidence while preserving the
    first insertion position.
    """
    records = []
    for column in ("subject", "object"):
        records += db.execute(
            f"SELECT evidence FROM edges WHERE {column}=? "
            "ORDER BY weight DESC, assertion LIMIT ?", (term, limit)).fetchall()
    edges = {}
    for (raw,) in records:
        edge = json.loads(raw)
        edges[edge["assertion"]] = edge
    return list(edges.values())


def node_degrees(source, labels, chunk=400):
    """Return global graph degree for ``labels`` using both endpoints.

    ``source`` may be an open read-only connection or an index path.  Degree is
    the number of indexed edges whose subject or object is the label; it is used
    only as a conservative hub filter, never as an evidence field.
    """
    labels = sorted({item for item in labels
                     if isinstance(item, str) and item})
    if not labels:
        return {}
    if isinstance(source, sqlite3.Connection):
        db = source
        close = False
    else:
        path = Path(source)
        if not path.exists():
            return {}
        db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        close = True
    try:
        degrees = {label: 0 for label in labels}
        for start in range(0, len(labels), max(1, int(chunk))):
            part = labels[start:start + max(1, int(chunk))]
            placeholders = ",".join("?" for _ in part)
            for column in ("subject", "object"):
                rows = db.execute(
                    f"SELECT {column}, count(*) FROM edges "
                    f"WHERE {column} IN ({placeholders}) GROUP BY {column}",
                    part).fetchall()
                for label, count in rows:
                    degrees[label] = degrees.get(label, 0) + int(count)
        return degrees
    finally:
        if close:
            db.close()


def _table_columns(db, table):
    return tuple(row[1] for row in db.execute(f"PRAGMA table_info({table})"))


def _index_names(db, table):
    return {row[1] for row in db.execute(f"PRAGMA index_list({table})")}


def validate_index(path, sample=1000):
    """Validate the structural and evidence contract of an index.

    The full edge table is not scanned; ``sample`` rows are checked for
    evidence/column agreement.  Returns a small report used by tests and the
    migration command.
    """
    path = Path(path)
    if not path.exists():
        raise ValueError(f"Index missing: {path}")
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        if _table_columns(db, "edges") != EDGE_COLUMNS:
            raise ValueError("Index edges table does not match the frozen columns")
        if _table_columns(db, "metadata") != ("key", "value"):
            raise ValueError("Index metadata table does not match the frozen columns")
        names = _index_names(db, "edges")
        for required in ("by_subject", "by_object"):
            if required not in names:
                raise ValueError(f"Index is missing {required}")
        manifest = read_manifest(db)
        missing = [key for key in MANIFEST_KEYS if key not in manifest]
        if missing:
            raise ValueError(f"Index manifest is missing keys: {missing}")
        if manifest["schema_version"] != INDEX_SCHEMA_VERSION:
            raise ValueError("Index manifest schema_version does not match")
        if not isinstance(manifest["relations"], list) or not isinstance(
                manifest["min_weight"], (int, float)):
            raise ValueError("Index manifest relations/min_weight are invalid")
        count = db.execute("SELECT count(*) FROM edges").fetchone()[0]
        if count != manifest["edges_indexed"]:
            raise ValueError("Index edge count does not match its manifest")
        sample = max(0, min(int(sample), count))
        checked = 0
        for assertion, subject, obj, weight, raw in db.execute(
                "SELECT assertion, subject, object, weight, evidence "
                "FROM edges LIMIT ?", (sample,)):
            try:
                evidence = json.loads(raw)
            except (TypeError, ValueError) as exc:
                raise ValueError("Index evidence is not valid JSON") from exc
            if not isinstance(evidence, dict):
                raise ValueError("Index evidence is not an object")
            for key, value in (("assertion", assertion), ("subject", subject),
                               ("object", obj), ("weight", weight)):
                if evidence.get(key) != value:
                    raise ValueError(f"Index evidence disagrees with column {key}")
            if "source_line" not in evidence:
                raise ValueError("Index evidence is missing source_line")
            if not isinstance(weight, (int, float)) or not math.isfinite(weight):
                raise ValueError("Index edge weight is not finite")
            checked += 1
        derived = derive_graph_id(manifest)
        row = db.execute("SELECT value FROM metadata WHERE key='graph_id'").fetchone()
        if row is not None and row[0] != derived:
            raise ValueError("Stored graph_id does not match the manifest")
        graph_id = row[0] if row is not None and row[0] else derived
        return {"path": str(path), "graph_id": graph_id, "edges": count,
                "sampled": checked, "manifest": manifest}


def ensure_graph_id(path, apply=False):
    """Add or verify the optional ``graph_id`` metadata row."""
    path = Path(path)
    if not path.exists():
        raise ValueError(f"Index missing: {path}")
    with closing(sqlite3.connect(path)) as db:
        manifest = read_manifest(db)
        graph_id = derive_graph_id(manifest)
        row = db.execute("SELECT value FROM metadata WHERE key='graph_id'").fetchone()
        if row is not None:
            if row[0] != graph_id:
                raise ValueError("Stored graph_id conflicts with the manifest")
            return {"graph_id": graph_id, "existing": True, "applied": False}
        if apply:
            db.execute("INSERT INTO metadata VALUES (?, ?)", ("graph_id", graph_id))
            db.commit()
        return {"graph_id": graph_id, "existing": False, "applied": bool(apply)}
