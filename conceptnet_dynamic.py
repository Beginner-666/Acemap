"""Query-time ConceptNet neighbors, with a reusable offline SQLite index."""

import csv
from contextlib import closing
import gzip
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import time
from urllib.parse import quote
from urllib.request import Request, urlopen
from urllib.error import URLError

import acemap_search as app


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
                s, o = app.concept(su, "en"), app.concept(ou, "en")
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
        metadata = {"schema_version": 1, "source_name": "ConceptNet 5.7 assertions",
                    "source_url": "https://github.com/commonsense/conceptnet5/wiki/Downloads",
                    "source_sha256": app.digest(source), "source_file": source.name,
                    "rows_scanned": rows, "edges_indexed": accepted,
                    "language": "en", "relations": sorted(RELATIONS), "min_weight": 1}
        db.execute("INSERT INTO metadata VALUES (?,?)", ("manifest", json.dumps(metadata)))
        db.commit()
    staging.replace(target)
    print(f"Index ready: {accepted:,} edges; {time.monotonic()-started:.1f}s; {target}")


def concept_forms(query):
    """Deterministic lexical normalization; not a claim of graph equivalence."""
    query = app.normalize(query)
    forms = [query]
    tokens = query.split()
    if "ai" in tokens:
        expanded = " ".join("artificial intelligence" if token == "ai" else token for token in tokens)
        if expanded != query:
            forms.append(expanded)
    return forms


class ConceptNet:
    def __init__(self, args):
        self.args = args
        self.online_failed = False

    def neighbors(self, term):
        key = hashlib.sha256((self.args.knowledge + "\0" + term).encode()).hexdigest()
        snapshot_path = self.args.conceptnet_cache / (key + ".json")
        if snapshot_path.exists() and not self.args.refresh:
            snapshot = app.read_json(snapshot_path)
            if snapshot["query"] != term:
                raise ValueError("ConceptNet cache query mismatch")
            return snapshot
        if self.args.cache_only:
            raise ValueError(f"Missing ConceptNet replay snapshot: {snapshot_path}")
        diagnostics = []
        edges = []
        matched = None
        backend = "none"
        source = None
        forms = concept_forms(term)
        for form in forms:
            if self.args.knowledge in ("auto", "online") and not self.online_failed:
                url = "https://api.conceptnet.io/c/en/" + quote(form.replace(" ", "_"), safe="") + "?limit=1000"
                try:
                    request = Request(url, headers={"User-Agent": app.USER_AGENT, "Accept": "application/json"})
                    with urlopen(request, timeout=min(self.args.timeout, 8)) as response:
                        payload = json.loads(response.read())
                    if not isinstance(payload, dict) or not isinstance(payload.get("edges"), list):
                        raise ValueError("Unknown ConceptNet API schema")
                    for item in payload["edges"]:
                        su, ou = item["start"]["@id"], item["end"]["@id"]
                        s, o = app.concept(su, "en"), app.concept(ou, "en")
                        weight = float(item["weight"])
                        if (item["rel"]["@id"] in RELATIONS and s and o and s != o
                                and form in (s, o) and math.isfinite(weight) and weight >= 1):
                            edges.append({"assertion": item["@id"], "relation": item["rel"]["@id"],
                                          "subject": s, "object": o, "subject_uri": su, "object_uri": ou,
                                          "weight": weight, "metadata": item})
                    backend, source = "online", {"url": url, "view": payload.get("view"), "limit": 1000}
                except (URLError, OSError, ValueError, KeyError, TypeError) as error:
                    diagnostics.append(f"ConceptNet online unavailable: {error}")
                    self.online_failed = True
            if not edges and self.args.knowledge in ("auto", "local"):
                if self.args.index.exists():
                    with closing(sqlite3.connect(self.args.index.resolve().as_uri() + "?mode=ro", uri=True)) as db:
                        manifest = json.loads(db.execute("SELECT value FROM metadata WHERE key='manifest'").fetchone()[0])
                        # Separate indexed endpoint queries avoid a full-table OR scan.
                        records = []
                        for column in ("subject", "object"):
                            records += db.execute(f"SELECT evidence FROM edges WHERE {column}=? ORDER BY weight DESC, assertion LIMIT 1000", (form,)).fetchall()
                        edges = list({edge["assertion"]: edge for (raw,) in records
                                      for edge in [json.loads(raw)]}.values())
                    backend, source = "local", manifest
                else:
                    diagnostics.append(f"Local index missing: {self.args.index}; run build-index once")
            if edges:
                matched = form
                break
        snapshot = {"query": term, "matched_concept": matched, "forms_checked": forms,
                    "backend": backend, "source": source, "diagnostics": diagnostics,
                    "edges": sorted(edges, key=app.edge_order),
                    "scope": "up to 1000 edges per endpoint; permitted positive relations, English"}
        # Failed service calls are not permanent negative cache entries.
        if backend != "none":
            app.write_json(snapshot_path, snapshot)
        return snapshot


def run_dynamic_search(args):
    queries = ([args.query] if args.query is not None else
               [line.strip() for line in args.queries.read_text(encoding="utf-8-sig").splitlines()
                if line.strip() and not line.lstrip().startswith("#")])
    queries = list(dict.fromkeys(map(app.normalize, queries)))
    if not queries or any(not term for term in queries):
        raise ValueError("Empty query")
    kg = ConceptNet(args)
    client = app.AcemapClient(args.cache, args.timeout, args.retries, args.cache_only, args.refresh)
    results = []
    for query in queries:
        try:
            snapshot = kg.neighbors(query)
        except (ValueError, OSError) as error:
            snapshot = {"query": query, "matched_concept": None, "backend": "none",
                        "edges": [], "diagnostics": [str(error)]}
        matched = snapshot["matched_concept"]
        graph = {"edges": snapshot["edges"]}
        expansion = app.expand(graph, matched, args.max_expansions) if matched else []
        expansion = [item for item in expansion if item["term"] != query]
        row = app.search_one(args, graph, client, query, expansion=expansion)
        row["conceptnet"] = snapshot
        if not expansion:
            row["warning"] = ("ConceptNet has no usable exact/normalized concept; original search only"
                              if snapshot["backend"] != "none" else "ConceptNet unavailable; original search only")
        results.append(row)
        print(f"{query}: kg={snapshot['backend']}; concept={matched}; expansions={[i['term'] for i in expansion]}; {row['status']}", flush=True)
    output = {"schema_version": 2, "domain": "query-time English ConceptNet", "endpoint": app.ENDPOINT,
              "settings": {"knowledge": args.knowledge, "size_per_term": args.size,
                           "max_expansions": args.max_expansions, "lexical_forms": "ai -> artificial intelligence; exact phrases only",
                           "fusion": "weighted RRF: original=1, expansion=0.5, k=60"},
              "summary": {"queries": len(results), "successful_queries": sum(r["status"] == "ok" for r in results),
                          "successful_with_enhancement": sum(r["status"] == "ok" and r["enhancement_applied"] for r in results)},
              "searches": results}
    app.write_json(args.output, output)
    return 0 if all(r["status"] == "ok" and r["conceptnet"]["backend"] != "none" for r in results) else 1
