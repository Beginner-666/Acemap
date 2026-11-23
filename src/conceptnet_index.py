"""Offline ConceptNet SQLite index construction."""

from __future__ import annotations

import csv
import gzip
import json
import math
from contextlib import closing
from pathlib import Path
import sqlite3
import time

from .graph_index import derive_graph_id
from .jsonio import digest
from .text import concept
from .versions import INDEX_SCHEMA_VERSION


# The dynamic pipeline only traverses general-purpose, positively weighted
# relations; the same set is used by the online and local backends.
RELATIONS = {"/r/IsA", "/r/PartOf", "/r/HasA", "/r/RelatedTo", "/r/Synonym",
             "/r/DerivedFrom", "/r/FormOf"}


def build_index(source, target):
    """Index all English edges for supported relations, independent of query/domain."""
    source, target = Path(source), Path(target)
    if target.exists():
        raise ValueError(f"Index already exists: {target}; choose a new output path")
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = target.with_suffix(".building.sqlite")
    if staging.exists():
        raise ValueError(f"Incomplete build exists: {staging}; choose another output path")
    started = time.monotonic()
    accepted = 0
    rows = 0
    with closing(sqlite3.connect(staging)) as db:
        db.execute("PRAGMA journal_mode=DELETE")
        db.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT)")
        db.execute("CREATE TABLE edges (assertion TEXT, subject TEXT, object TEXT, weight REAL, evidence TEXT)")
        pending = []
        csv.field_size_limit(16 * 1024 * 1024)
        opener = gzip.open if source.suffix == ".gz" else open
        with opener(source, "rt", encoding="utf-8", newline="") as stream:
            for rows, row in enumerate(csv.reader(stream, delimiter="\t"), 1):
                if rows % 2_000_000 == 0:
                    print(f"Index: scanned {rows:,}; kept {accepted:,}; {time.monotonic()-started:.1f}s", flush=True)
                if len(row) != 5 or row[1] not in RELATIONS:
                    continue
                assertion, relation, su, ou, raw = row
                s, o = concept(su, "en"), concept(ou, "en")
                if not s or not o or s == o:
                    continue
                try:
                    metadata = json.loads(raw)
                    weight = float(metadata["weight"])
                except (ValueError, KeyError, TypeError):
                    continue
                if not math.isfinite(weight) or weight < 1:
                    continue
                edge = {"assertion": assertion, "relation": relation, "subject": s, "object": o,
                        "subject_uri": su, "object_uri": ou, "weight": weight,
                        "source_line": rows, "metadata": metadata}
                pending.append((assertion, s, o, weight, json.dumps(edge, ensure_ascii=False)))
                accepted += 1
                if len(pending) >= 5000:
                    db.executemany("INSERT INTO edges VALUES (?,?,?,?,?)", pending)
                    db.commit()
                    pending.clear()
        db.executemany("INSERT INTO edges VALUES (?,?,?,?,?)", pending)
        print("Building neighbor indexes...", flush=True)
        db.execute("CREATE INDEX by_subject ON edges(subject, weight DESC, assertion)")
        db.execute("CREATE INDEX by_object ON edges(object, weight DESC, assertion)")
        metadata = {"schema_version": INDEX_SCHEMA_VERSION, "source_name": "ConceptNet 5.7 assertions",
                    "source_url": "https://github.com/commonsense/conceptnet5/wiki/Downloads",
                    "source_sha256": digest(source), "source_file": source.name,
                    "rows_scanned": rows, "edges_indexed": accepted,
                    "language": "en", "relations": sorted(RELATIONS), "min_weight": 1}
        db.execute("INSERT INTO metadata VALUES (?,?)", ("manifest", json.dumps(metadata)))
        # Keep graph_id in a separate metadata row: the local backend embeds
        # the manifest in v2 snapshots, so the manifest shape is frozen.
        db.execute("INSERT INTO metadata VALUES (?,?)", ("graph_id", derive_graph_id(metadata)))
        db.commit()
    staging.replace(target)
    print(f"Index ready: {accepted:,} edges; {time.monotonic()-started:.1f}s; {target}")
