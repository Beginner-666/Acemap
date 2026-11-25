"""Offline ConceptNet graph cleaning and extension ingestion (``clean-v1``).

The cleaned graph is an independent SQLite artifact.  The default search path
still reads ``data/conceptnet_en.sqlite``; callers opt in explicitly by passing
``--index data/conceptnet_clean.sqlite``.  Raw assertions and the raw index are
never modified.

See ``docs/interface_protocol.md`` §6.3 and §7.5.
"""

from __future__ import annotations

import csv
import gzip
import hashlib
import heapq
import json
import math
import sqlite3
from contextlib import closing
from pathlib import Path
from urllib.parse import quote

from .conceptnet_index import RELATIONS
from .graph_index import derive_graph_id, read_graph_id, read_manifest
from .jsonio import digest, write_json
from .text import concept, normalize
from .versions import (CLEAN_GRAPH_RULES_VERSION, GRAPH_RULES_VERSION,
                       INDEX_SCHEMA_VERSION, canonical_json, sha256_text)

QUALITY_SCHEMA = "graph-quality-v1"
EXTENSION_SCHEMA = "extensions-v1"
CONCEPTNET_SOURCE = "ConceptNet 5.7 assertions"
EXTENSION_SOURCE_TYPES = ("domain_glossary", "public_ontology", "paper_concepts",
                          "human_review", "online_api")
_SQLITE_SUFFIXES = {".sqlite", ".sqlite3", ".db"}
CLEANING_RULE_KEYS = (
    "invalid_rows", "invalid_evidence", "invalid_assertion", "invalid_uri",
    "non_english", "empty_label", "disallowed_relation", "self_loop",
    "non_finite_weight", "invalid_metadata", "low_weight", "duplicate_edges",
    "degree_cap_edges", "domain_low_confidence",
)


class _FixedSample:
    """Deterministic smallest-hash sample without keeping all input rows."""

    def __init__(self, size):
        self.size = max(0, int(size))
        self.items = []
        self.sequence = 0

    def add(self, identity, item):
        if self.size <= 0:
            return
        token = sha256_text(identity)
        order = int(token, 16)
        self.sequence += 1
        row = (-order, token, self.sequence, item)
        if len(self.items) < self.size:
            heapq.heappush(self.items, row)
            return
        if order < -self.items[0][0]:
            heapq.heapreplace(self.items, row)

    def ordered(self):
        return [row[3] for row in
                sorted(self.items, key=lambda row: (row[0], row[1], row[2]))]


def _bump(counts, key, amount=1):
    counts[key] = counts.get(key, 0) + amount


def _sample_key(assertion, subject, relation, obj):
    return "\0".join(str(value or "") for value in
                     (assertion, subject, relation, obj))


def _sample_summary(edge):
    summary = {
        "assertion": edge.get("assertion"),
        "relation": edge.get("relation"),
        "subject": edge.get("subject"),
        "object": edge.get("object"),
        "subject_uri": edge.get("subject_uri"),
        "object_uri": edge.get("object_uri"),
        "weight": edge.get("weight"),
        "source_line": edge.get("source_line"),
    }
    for key in ("source_lines", "confidence", "domain_relevant"):
        if key in edge:
            summary[key] = edge[key]
    return summary


def _safe_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _is_cross_language(uri):
    if not isinstance(uri, str):
        return False
    parts = uri.split("/")
    return len(parts) >= 3 and parts[1] == "c" and parts[2] != "en"


def _validate_base_edge(raw, min_weight, counts):
    """Validate one raw ConceptNet edge and return a normalized edge or None.

    Every rejection increments a named rule counter so the quality report can
    explain exactly what was removed.  Invalid input never raises.
    """
    assertion = raw.get("assertion")
    relation = raw.get("relation")
    subject_uri = raw.get("subject_uri")
    object_uri = raw.get("object_uri")
    metadata = raw.get("metadata")
    weight = _safe_float(raw.get("weight"))
    source_line = _safe_int(raw.get("source_line"), 0)

    if _is_cross_language(subject_uri) or _is_cross_language(object_uri):
        _bump(counts, "cross_language_edges")
    if not isinstance(assertion, str) or not assertion:
        _bump(counts, "invalid_assertion")
        return None
    if not isinstance(relation, str) or relation not in RELATIONS:
        _bump(counts, "disallowed_relation")
        return None
    if not isinstance(subject_uri, str) or not subject_uri:
        _bump(counts, "invalid_uri")
        return None
    if not isinstance(object_uri, str) or not object_uri:
        _bump(counts, "invalid_uri")
        return None
    if not (subject_uri.startswith("/c/") and object_uri.startswith("/c/")):
        _bump(counts, "invalid_uri")
        return None
    subject = concept(subject_uri, "en")
    obj = concept(object_uri, "en")
    if subject is None or obj is None:
        _bump(counts, "non_english")
        return None
    if not subject or not obj:
        _bump(counts, "empty_label")
        return None
    if subject == obj:
        _bump(counts, "self_loop")
        return None
    if not isinstance(metadata, dict):
        _bump(counts, "invalid_metadata")
        return None
    if weight is None or not math.isfinite(weight):
        _bump(counts, "non_finite_weight")
        return None
    if weight < min_weight:
        _bump(counts, "low_weight")
        return None
    return {
        "assertion": assertion,
        "relation": relation,
        "subject": subject,
        "object": obj,
        "subject_uri": subject_uri,
        "object_uri": object_uri,
        "weight": weight,
        "metadata": metadata,
        "source_line": source_line,
        "dataset": metadata.get("dataset") if isinstance(metadata.get("dataset"), str) else None,
        "license": metadata.get("license") if isinstance(metadata.get("license"), str) else None,
        "source": CONCEPTNET_SOURCE,
        "source_type": "conceptnet",
        "created_at": None,
        "confidence": 1.0,
    }


def _new_group(edge):
    return {
        "assertion": edge["assertion"],
        "relation": edge["relation"],
        "subject": edge["subject"],
        "object": edge["object"],
        "subject_uri": edge["subject_uri"],
        "object_uri": edge["object_uri"],
        "weight": edge["weight"],
        "metadata": edge["metadata"],
        "source_line": edge["source_line"],
        "source_lines": [edge["source_line"]] if edge.get("source_line") else [],
        "source_assertions": [edge["assertion"]],
        "subject_uris": [edge["subject_uri"]],
        "object_uris": [edge["object_uri"]],
        "datasets": sorted({edge["dataset"]}) if edge.get("dataset") else [],
        "licenses": sorted({edge["license"]}) if edge.get("license") else [],
        "dataset": edge.get("dataset"),
        "license": edge.get("license"),
        "source_file": edge.get("source_file"),
        "sources": [edge.get("source") or CONCEPTNET_SOURCE],
        "source_types": [edge.get("source_type") or "conceptnet"],
        "source": edge.get("source") or CONCEPTNET_SOURCE,
        "source_type": edge.get("source_type") or "conceptnet",
        "created_at": edge.get("created_at"),
        "confidence": float(edge.get("confidence", 1.0)),
        "domain_relevant": None,
        "duplicate_count": 0,
    }


def _append_unique(values, additions):
    for value in additions:
        if value is not None and value not in values:
            values.append(value)


def _merge_group(group, edge, counts, extension=False):
    """Merge one duplicate into a representative group, preserving provenance."""
    _bump(counts, "duplicate_edges")
    _bump(counts, "extension_merged_edges" if extension else "base_duplicate_edges")
    group["duplicate_count"] += 1
    if edge.get("source_line"):
        _append_unique(group["source_lines"], [edge["source_line"]])
    _append_unique(group["source_assertions"], [edge["assertion"]])
    _append_unique(group["subject_uris"], [edge["subject_uri"]])
    _append_unique(group["object_uris"], [edge["object_uri"]])
    _append_unique(group["datasets"], [edge.get("dataset")])
    _append_unique(group["licenses"], [edge.get("license")])
    _append_unique(group["sources"], [edge.get("source")])
    _append_unique(group["source_types"], [edge.get("source_type")])
    if edge.get("created_at") and not group.get("created_at"):
        group["created_at"] = edge["created_at"]
    # Keep the highest weight; ties use the lexicographically smallest
    # assertion so the representative does not depend on input order.
    replace = (edge["weight"] > group["weight"]
               or (edge["weight"] == group["weight"]
                   and edge["assertion"] < group["assertion"]))
    if replace:
        for key in ("assertion", "relation", "subject", "object", "subject_uri",
                    "object_uri", "weight", "metadata", "source_line", "created_at"):
            group[key] = edge.get(key)
        group["confidence"] = float(edge.get("confidence", 1.0))
        if edge.get("dataset"):
            group["dataset"] = edge["dataset"]
        if edge.get("license"):
            group["license"] = edge["license"]
        if edge.get("source"):
            group["source"] = edge["source"]
        if edge.get("source_type"):
            group["source_type"] = edge["source_type"]


def _read_index_edges(path, counts, sample):
    """Yield raw edges from an existing SQLite index without modifying it."""
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        for assertion, subject, obj, weight, raw in db.execute(
                "SELECT assertion, subject, object, weight, evidence FROM edges"):
            _bump(counts, "input_rows")
            try:
                evidence = json.loads(raw)
            except (TypeError, ValueError):
                _bump(counts, "invalid_evidence")
                continue
            if not isinstance(evidence, dict):
                _bump(counts, "invalid_evidence")
                continue
            edge = {
                "assertion": evidence.get("assertion", assertion),
                "relation": evidence.get("relation"),
                "subject": evidence.get("subject", subject),
                "object": evidence.get("object", obj),
                "subject_uri": evidence.get("subject_uri"),
                "object_uri": evidence.get("object_uri"),
                "weight": evidence.get("weight", weight),
                "metadata": evidence.get("metadata"),
                "source_line": evidence.get("source_line", 0),
            }
            sample.add(_sample_key(edge["assertion"], edge["subject"],
                                   edge["relation"], edge["object"]),
                       _sample_summary(edge))
            yield edge


def _read_assertion_edges(path, counts, sample):
    """Yield raw edges from a ConceptNet TSV/CSV.gz assertions file."""
    csv.field_size_limit(16 * 1024 * 1024)
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8", newline="") as stream:
        for line_number, row in enumerate(csv.reader(stream, delimiter="\t"), 1):
            _bump(counts, "input_rows")
            if len(row) != 5:
                _bump(counts, "invalid_rows")
                continue
            assertion, relation, subject_uri, object_uri, raw = row
            metadata = None
            weight = None
            try:
                metadata = json.loads(raw)
                if isinstance(metadata, dict):
                    weight = float(metadata["weight"])
            except (TypeError, ValueError, KeyError):
                metadata, weight = None, None
            edge = {
                "assertion": assertion,
                "relation": relation,
                "subject": concept(subject_uri, "en"),
                "object": concept(object_uri, "en"),
                "subject_uri": subject_uri,
                "object_uri": object_uri,
                "weight": weight,
                "metadata": metadata,
                "source_line": line_number,
            }
            sample.add(_sample_key(assertion, subject_uri, relation, object_uri),
                       _sample_summary(edge))
            yield edge


def _apply_degree_cap(groups, cap, counts):
    if cap is None:
        return
    incident = {}
    for key, edge in groups.items():
        incident.setdefault(edge["subject"], []).append(key)
        incident.setdefault(edge["object"], []).append(key)
    dropped = set()
    for keys in incident.values():
        if len(keys) <= cap:
            continue
        _bump(counts, "degree_cap_nodes")
        ordered = sorted(keys, key=lambda key: (
            -groups[key]["weight"], groups[key]["assertion"],
            groups[key]["subject"], groups[key]["object"]))
        dropped.update(ordered[cap:])
    _bump(counts, "degree_cap_edges", len(dropped))
    for key in dropped:
        del groups[key]


def _load_domain_terms(path):
    if path is None:
        return set()
    path = Path(path)
    if not path.exists():
        raise ValueError(f"Domain terms file missing: {path}")
    terms = set()
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        term = normalize(line)
        if term:
            terms.add(term)
    if not terms:
        raise ValueError(f"Domain terms file is empty: {path}")
    return terms


def _apply_domain(edges, domain_terms, counts):
    if not domain_terms:
        for edge in edges:
            edge["domain_relevant"] = None
        return None
    relevant = 0
    for edge in edges:
        is_relevant = (edge["subject"] in domain_terms
                       or edge["object"] in domain_terms)
        edge["domain_relevant"] = is_relevant
        if is_relevant:
            relevant += 1
        else:
            _bump(counts, "domain_low_confidence")
            edge["confidence"] = min(float(edge.get("confidence", 1.0)), 0.5)
    return relevant / len(edges) if edges else 0.0


def _edge_sort_key(edge):
    return (edge["subject"], edge["relation"], edge["object"], edge["assertion"])


def _evidence(edge):
    """Build the frozen-shape evidence object plus provenance fields."""
    subject_uris = sorted(set(edge.get("subject_uris") or [edge["subject_uri"]]))
    object_uris = sorted(set(edge.get("object_uris") or [edge["object_uri"]]))
    datasets = edge.get("datasets") or ([edge["dataset"]] if edge.get("dataset") else [])
    licenses = edge.get("licenses") or ([edge["license"]] if edge.get("license") else [])
    sources = edge.get("sources") or ([edge["source"]] if edge.get("source") else [])
    source_types = (edge.get("source_types")
                    or ([edge["source_type"]] if edge.get("source_type") else []))
    source_assertions = edge.get("source_assertions") or [edge["assertion"]]
    source_lines = edge.get("source_lines")
    if source_lines is None:
        source_lines = [edge["source_line"]] if edge.get("source_line") else []
    return {
        "assertion": edge["assertion"],
        "relation": edge["relation"],
        "subject": edge["subject"],
        "object": edge["object"],
        "subject_uri": edge["subject_uri"],
        "object_uri": edge["object_uri"],
        "weight": edge["weight"],
        "metadata": edge["metadata"],
        "source_line": edge["source_line"],
        "source_lines": sorted(set(source_lines)),
        "source_assertions": sorted(set(source_assertions)),
        "aliases": {"subject": subject_uris, "object": object_uris},
        "dataset": edge.get("dataset"),
        "license": edge.get("license"),
        "source_file": edge.get("source_file"),
        "datasets": sorted(set(datasets)),
        "licenses": sorted(set(licenses)),
        "sources": sorted(set(sources)),
        "source_types": sorted(set(source_types)),
        "source": edge.get("source"),
        "source_type": edge.get("source_type"),
        "created_at": edge.get("created_at"),
        "confidence": float(edge.get("confidence", 1.0)),
        "domain_relevant": edge.get("domain_relevant"),
        "rules_version": CLEAN_GRAPH_RULES_VERSION,
    }


def _content_hash(edges):
    result = hashlib.sha256()
    result.update(b"clean-graph-v1\0")
    for edge in edges:
        result.update(canonical_json(_evidence(edge)).encode("utf-8"))
        result.update(b"\n")
    return result.hexdigest()


def _load_extension_file(path, min_weight, counts):
    """Load and validate one JSON/JSONL extension file."""
    path = Path(path)
    if not path.exists():
        raise ValueError(f"Extension file missing: {path}")
    raw_bytes = path.read_bytes()
    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"Extension file is not UTF-8: {path}") from exc
    payload = None
    try:
        payload = json.loads(text)
    except ValueError:
        payload = None
    if isinstance(payload, dict) and ("edges" in payload or "extensions" in payload):
        items = payload.get("edges", payload.get("extensions", []))
        file_source_type = payload.get("source_type")
        if not isinstance(items, list):
            raise ValueError(f"Extension JSON edges must be a list: {path}")
    elif isinstance(payload, dict):
        items = [payload]
        file_source_type = payload.get("source_type")
    elif isinstance(payload, list):
        items = payload
        file_source_type = None
    else:
        items = []
        file_source_type = None
        for line_number, line in enumerate(text.splitlines(), 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                item = json.loads(line)
            except ValueError as exc:
                _bump(counts, "extension_invalid_json")
                continue
            if not isinstance(item, dict):
                _bump(counts, "extension_invalid_json")
                continue
            items.append(item)
    accepted = []
    for item in items:
        _bump(counts, "extension_input_rows")
        if not isinstance(item, dict):
            _bump(counts, "extension_invalid_json")
            continue
        source_type = item.get("source_type", file_source_type)
        if source_type not in EXTENSION_SOURCE_TYPES:
            _bump(counts, "extension_invalid_source_type")
            continue
        subject = item.get("subject")
        obj = item.get("object")
        relation = item.get("relation")
        weight = _safe_float(item.get("weight"))
        confidence = _safe_float(item.get("confidence"))
        source = item.get("source")
        license_name = item.get("license")
        created_at = item.get("created_at")
        if not isinstance(subject, str) or not isinstance(obj, str):
            _bump(counts, "extension_invalid_label")
            continue
        subject, obj = normalize(subject), normalize(obj)
        if not subject or not obj:
            _bump(counts, "extension_invalid_label")
            continue
        if subject == obj:
            _bump(counts, "extension_self_loop")
            continue
        if not isinstance(relation, str) or relation not in RELATIONS:
            _bump(counts, "extension_invalid_relation")
            continue
        if weight is None or not math.isfinite(weight) or weight < min_weight:
            _bump(counts, "extension_invalid_weight")
            continue
        if confidence is None or not math.isfinite(confidence) or not 0 <= confidence <= 1:
            _bump(counts, "extension_invalid_confidence")
            continue
        if not isinstance(source, str) or not source:
            _bump(counts, "extension_missing_source")
            continue
        if not isinstance(license_name, str) or not license_name:
            _bump(counts, "extension_missing_license")
            continue
        if not isinstance(created_at, str) or not created_at:
            _bump(counts, "extension_missing_created_at")
            continue
        subject_uri = item.get("subject_uri")
        object_uri = item.get("object_uri")
        if subject_uri is None:
            subject_uri = "/c/en/" + quote(subject.replace(" ", "_"), safe="")
        if object_uri is None:
            object_uri = "/c/en/" + quote(obj.replace(" ", "_"), safe="")
        if concept(subject_uri, "en") != subject or concept(object_uri, "en") != obj:
            _bump(counts, "extension_uri_mismatch")
            continue
        assertion = item.get("assertion")
        if not isinstance(assertion, str) or not assertion:
            assertion = "/x/ext/" + sha256_text(canonical_json({
                "subject": subject, "relation": relation, "object": obj,
                "source": source, "created_at": created_at,
            }))[:16]
        metadata = item.get("metadata")
        if not isinstance(metadata, dict):
            metadata = {}
        edge = {
            "assertion": assertion,
            "relation": relation,
            "subject": subject,
            "object": obj,
            "subject_uri": subject_uri,
            "object_uri": object_uri,
            "weight": weight,
            "metadata": metadata,
            "source_line": _safe_int(item.get("source_line"), 0),
            "dataset": source,
            "license": license_name,
            "source": source,
            "source_type": source_type,
            "created_at": created_at,
            "confidence": confidence,
            "source_file": path.name,
        }
        accepted.append(edge)
    return accepted, raw_bytes


def load_extensions(paths, min_weight, counts):
    """Load all extension inputs and return normalized edges plus a manifest."""
    inputs = []
    edges = []
    unique_paths = []
    for item in paths:
        path = Path(item)
        if path not in unique_paths:
            unique_paths.append(path)
    for path in sorted(unique_paths, key=lambda item: (item.name, str(item))):
        accepted, raw_bytes = _load_extension_file(path, min_weight, counts)
        _bump(counts, "extension_accepted", len(accepted))
        inputs.append({
            "file": path.name,
            "sha256": hashlib.sha256(raw_bytes).hexdigest(),
            "source_types": sorted({edge["source_type"] for edge in accepted}),
            "rows": len(accepted),
        })
        edges.extend(accepted)
    manifest = {
        "schema_version": EXTENSION_SCHEMA,
        "rules_version": CLEAN_GRAPH_RULES_VERSION,
        "source_priority": list(EXTENSION_SOURCE_TYPES),
        "inputs": inputs,
        "edges": len(edges),
        "nodes": len({value for edge in edges
                      for value in (edge["subject"], edge["object"])}),
        "licenses": sorted({edge["license"] for edge in edges}),
    }
    return edges, manifest


def _write_extensions_table(db, edges):
    db.execute("""CREATE TABLE extensions (
        assertion TEXT, subject TEXT, object TEXT, relation TEXT, weight REAL,
        confidence REAL, source TEXT, source_type TEXT, license TEXT,
        created_at TEXT, source_file TEXT, source_line INTEGER, evidence TEXT)""")
    db.execute("CREATE INDEX by_extension_subject ON extensions(subject, weight DESC, assertion)")
    db.execute("CREATE INDEX by_extension_object ON extensions(object, weight DESC, assertion)")
    rows = []
    for edge in sorted(edges, key=_edge_sort_key):
        rows.append((
            edge["assertion"], edge["subject"], edge["object"], edge["relation"],
            edge["weight"], edge["confidence"], edge["source"], edge["source_type"],
            edge["license"], edge["created_at"],
            edge.get("source_file") or edge["source"], edge["source_line"],
            json.dumps(_evidence(edge), ensure_ascii=False),
        ))
    db.executemany("INSERT INTO extensions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)


def _degree_summary(degrees):
    if not degrees:
        return {"nodes": 0, "min": 0, "median": 0, "p90": 0, "max": 0}
    ordered = sorted(degrees)
    return {
        "nodes": len(ordered),
        "min": ordered[0],
        "median": ordered[len(ordered) // 2],
        "p90": ordered[min(len(ordered) - 1, int(round((len(ordered) - 1) * 0.9)))],
        "max": ordered[-1],
    }


def _build_manifest(base_manifest, input_path, stats, cleaning_parameters,
                    min_weight):
    manifest = dict(base_manifest)
    manifest["schema_version"] = INDEX_SCHEMA_VERSION
    manifest["language"] = "en"
    manifest["relations"] = sorted(RELATIONS)
    manifest["min_weight"] = min_weight
    manifest["rows_scanned"] = stats.get("input_rows", 0)
    manifest["edges_indexed"] = 0
    if input_path.suffix.lower() in _SQLITE_SUFFIXES:
        manifest.setdefault("source_name", CONCEPTNET_SOURCE)
        manifest.setdefault("source_url", "https://github.com/commonsense/conceptnet5/wiki/Downloads")
        manifest.setdefault("source_sha256", digest(input_path))
        manifest.setdefault("source_file", input_path.name)
        manifest["rows_scanned"] = stats.get("input_rows", 0)
    else:
        manifest.update({
            "source_name": manifest.get("source_name", CONCEPTNET_SOURCE),
            "source_url": manifest.get("source_url",
                                       "https://github.com/commonsense/conceptnet5/wiki/Downloads"),
            "source_sha256": digest(input_path),
            "source_file": input_path.name,
        })
    if cleaning_parameters["extensions_sha256"]:
        manifest["source_name"] = str(manifest["source_name"]) + " + extensions"
        manifest["source_file"] = str(manifest["source_file"]) + " + extensions"
    manifest["edges_indexed"] = stats["output_edges"]
    return manifest


def clean_graph(input_path, output_path, *, min_weight=1.0, degree_cap=500,
                domain_terms=None, extensions=(), merge_extensions=False,
                quality_report=None, sample_size=20, verbose=False):
    """Build a deterministic cleaned graph and write its quality report."""
    input_path, output_path = Path(input_path), Path(output_path)
    if not input_path.exists():
        raise ValueError(f"Input graph missing: {input_path}")
    if output_path.exists():
        raise ValueError(f"Output already exists: {output_path}; choose a new path")
    if input_path.resolve() == output_path.resolve():
        raise ValueError("Clean graph must not overwrite its input")
    if degree_cap is not None and (isinstance(degree_cap, bool)
                                   or not isinstance(degree_cap, int) or degree_cap < 1):
        raise ValueError("degree_cap must be null or a positive integer")
    min_weight = float(min_weight)
    if not math.isfinite(min_weight) or min_weight < 0:
        raise ValueError("min_weight must be a finite non-negative number")

    if verbose:
        print(f"Cleaning {input_path.name} -> {output_path.name}", flush=True)
    counts = {key: 0 for key in CLEANING_RULE_KEYS}
    before_sample = _FixedSample(sample_size)
    groups = {}
    input_nodes = set()
    base_manifest = {}
    parent_graph_id = None

    if input_path.suffix.lower() in _SQLITE_SUFFIXES:
        with closing(sqlite3.connect(input_path.resolve().as_uri() + "?mode=ro",
                                     uri=True)) as db:
            base_manifest = read_manifest(db)
            parent_graph_id = read_graph_id(db)
        reader = _read_index_edges(input_path, counts, before_sample)
    else:
        reader = _read_assertion_edges(input_path, counts, before_sample)

    for raw in reader:
        _bump(counts, "input_edges")
        edge = _validate_base_edge(raw, min_weight, counts)
        if edge is None:
            continue
        input_nodes.update((edge["subject"], edge["object"]))
        key = (edge["subject"], edge["relation"], edge["object"])
        existing = groups.get(key)
        if existing is None:
            groups[key] = _new_group(edge)
        else:
            _merge_group(existing, edge, counts)
    if verbose:
        print(f"Clean: scanned {counts.get('input_rows', 0):,} rows; "
              f"{len(groups):,} unique triples", flush=True)

    extension_edges, extensions_manifest = load_extensions(
        extensions, min_weight, counts) if extensions else ([], {
            "schema_version": EXTENSION_SCHEMA,
            "rules_version": CLEAN_GRAPH_RULES_VERSION,
            "source_priority": list(EXTENSION_SOURCE_TYPES),
            "inputs": [], "edges": 0, "nodes": 0, "licenses": [],
        })
    if merge_extensions and extension_edges:
        for edge in extension_edges:
            input_nodes.update((edge["subject"], edge["object"]))
            key = (edge["subject"], edge["relation"], edge["object"])
            existing = groups.get(key)
            if existing is None:
                groups[key] = _new_group(edge)
            else:
                _merge_group(existing, edge, counts, extension=True)
    _bump(counts, "duplicate_groups",
          sum(1 for edge in groups.values() if edge["duplicate_count"]))
    _apply_degree_cap(groups, degree_cap, counts)
    final_edges = sorted(groups.values(), key=_edge_sort_key)
    if verbose:
        print(f"Clean: {len(final_edges):,} edges after degree cap; "
              f"{counts.get('degree_cap_edges', 0):,} dropped", flush=True)
    domain_terms = _load_domain_terms(domain_terms) if domain_terms else set()
    domain_coverage = _apply_domain(final_edges, domain_terms, counts)
    after_sample = _FixedSample(sample_size)
    for edge in final_edges:
        after_sample.add(_sample_key(edge["assertion"], edge["subject"],
                                     edge["relation"], edge["object"]),
                         _sample_summary(edge))

    node_set = set()
    relation_distribution = {}
    incident = {}
    for edge in final_edges:
        node_set.update((edge["subject"], edge["object"]))
        incident.setdefault(edge["subject"], 0)
        incident[edge["subject"]] += 1
        incident.setdefault(edge["object"], 0)
        incident[edge["object"]] += 1
        relation_distribution[edge["relation"]] = (
            relation_distribution.get(edge["relation"], 0) + 1)
    counts["output_edges"] = len(final_edges)
    counts["output_nodes"] = len(node_set)
    counts["isolated_nodes"] = len(input_nodes - node_set)
    content_sha256 = _content_hash(final_edges)

    if parent_graph_id is None:
        parent_manifest = {
            "source_name": CONCEPTNET_SOURCE,
            "source_sha256": digest(input_path),
            "language": "en",
            "relations": sorted(RELATIONS),
            "min_weight": min_weight,
        }
        parent_graph_id = derive_graph_id(parent_manifest, GRAPH_RULES_VERSION)
    licenses = sorted({license_name for edge in final_edges
                       for license_name in edge.get("licenses", [])})
    datasets = sorted({dataset for edge in final_edges
                       for dataset in edge.get("datasets", [])})
    extensions_sha256 = (sha256_text(canonical_json(extensions_manifest))
                         if merge_extensions and extensions_manifest["edges"] else None)
    domain_terms_sha256 = (sha256_text(canonical_json(sorted(domain_terms)))
                           if domain_terms else None)
    cleaning_parameters = {
        "rules_version": CLEAN_GRAPH_RULES_VERSION,
        "min_weight": min_weight,
        "degree_cap": degree_cap,
        "domain_terms_sha256": domain_terms_sha256,
        "extensions_sha256": extensions_sha256,
    }
    manifest = _build_manifest(base_manifest, input_path, counts,
                               cleaning_parameters, min_weight)
    clean_parameters_sha256 = sha256_text(canonical_json(cleaning_parameters))
    graph_id = derive_graph_id(manifest, CLEAN_GRAPH_RULES_VERSION,
                               clean_parameters_sha256)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    staging = output_path.with_suffix(".building.sqlite")
    if staging.exists():
        raise ValueError(f"Incomplete build exists: {staging}; choose another output path")
    with closing(sqlite3.connect(staging)) as db:
        db.execute("PRAGMA journal_mode=DELETE")
        db.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT)")
        db.execute("CREATE TABLE edges (assertion TEXT, subject TEXT, object TEXT, weight REAL, evidence TEXT)")
        rows = []
        for edge in final_edges:
            rows.append((edge["assertion"], edge["subject"], edge["object"],
                         edge["weight"], json.dumps(_evidence(edge), ensure_ascii=False)))
            if len(rows) >= 5000:
                db.executemany("INSERT INTO edges VALUES (?,?,?,?,?)", rows)
                rows.clear()
        db.executemany("INSERT INTO edges VALUES (?,?,?,?,?)", rows)
        db.execute("CREATE INDEX by_subject ON edges(subject, weight DESC, assertion)")
        db.execute("CREATE INDEX by_object ON edges(object, weight DESC, assertion)")
        _write_extensions_table(db, extension_edges)
        metadata = {
            "manifest": json.dumps(manifest),
            "graph_id": graph_id,
            "rules_version": CLEAN_GRAPH_RULES_VERSION,
            "parent_graph_id": parent_graph_id,
            "licenses": json.dumps(licenses),
            "datasets": json.dumps(datasets),
            "content_sha256": content_sha256,
            "extensions": json.dumps(extensions_manifest, sort_keys=True),
            "extensions_merged": "true" if merge_extensions and extensions_manifest["edges"] else "false",
            "cleaning_parameters": json.dumps(cleaning_parameters, sort_keys=True),
            "clean_parameters_sha256": clean_parameters_sha256,
            "cleaning_report": json.dumps({
                "rules_version": CLEAN_GRAPH_RULES_VERSION,
                "content_sha256": content_sha256,
                "counts": dict(sorted(counts.items())),
                "cleaning_parameters": cleaning_parameters,
                "clean_parameters_sha256": clean_parameters_sha256,
                "extensions_sha256": extensions_sha256,
                "extensions_merged": bool(merge_extensions and extensions_manifest["edges"]),
            }, sort_keys=True),
        }
        if extensions_sha256:
            metadata["extensions_sha256"] = extensions_sha256
        for key, value in metadata.items():
            db.execute("INSERT INTO metadata VALUES (?,?)", (key, value))
        db.commit()
    staging.replace(output_path)
    if verbose:
        print(f"Clean graph ready: {len(final_edges):,} edges; graph_id={graph_id}",
              flush=True)

    report = {
        "schema_version": QUALITY_SCHEMA,
        "rules_version": CLEAN_GRAPH_RULES_VERSION,
        "input": {
            "file": input_path.name,
            "sha256": digest(input_path),
            "kind": "sqlite" if input_path.suffix.lower() in _SQLITE_SUFFIXES else "assertions",
        },
        "output": {
            "file": output_path.name,
            "graph_id": graph_id,
            "parent_graph_id": parent_graph_id,
            "content_sha256": content_sha256,
            "edges": len(final_edges),
            "nodes": len(node_set),
        },
        "manifest": manifest,
        "cleaning_parameters": cleaning_parameters,
        "clean_parameters_sha256": clean_parameters_sha256,
        "counts": dict(sorted(counts.items())),
        "rules": {key: counts[key] for key in CLEANING_RULE_KEYS},
        "relations": dict(sorted(relation_distribution.items())),
        "degrees": _degree_summary(list(incident.values())),
        "duplicate_rate": (counts.get("duplicate_edges", 0)
                           / max(1, counts.get("input_edges", 0))),
        "cross_language_edges": counts.get("cross_language_edges", 0),
        "low_weight_edges": counts.get("low_weight", 0),
        "domain_coverage": domain_coverage,
        "extensions": extensions_manifest,
        "samples": {
            "method": "sha256(assertion, subject, relation, object) smallest first",
            "before": before_sample.ordered(),
            "after": after_sample.ordered(),
            "conclusion": "pending manual review",
        },
    }
    if quality_report is not None:
        write_json(Path(quality_report), report)
    return report
