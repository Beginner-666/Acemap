"""Query-time ConceptNet neighbors with online and offline backends."""

from __future__ import annotations

import hashlib
import json
import math
from contextlib import closing
from pathlib import Path
import sqlite3
from urllib.parse import quote

from . import net
from .conceptnet_index import RELATIONS
from .constants import USER_AGENT
from .edges import edge_order
from .jsonio import read_json, write_json
from .text import concept, normalize


def concept_forms(query):
    """Deterministic lexical normalization; not a claim of graph equivalence."""
    query = normalize(query)
    forms = [query]
    tokens = query.split()
    if "ai" in tokens:
        expanded = " ".join("artificial intelligence" if token == "ai" else token for token in tokens)
        if expanded != query:
            forms.append(expanded)
    return forms


class ConceptNet:
    """Fetch one-hop English neighbors for a query, caching successful lookups."""

    def __init__(self, knowledge, index, cache_dir, timeout,
                 refresh=False, cache_only=False):
        self.knowledge = knowledge
        self.index = Path(index)
        self.cache_dir = Path(cache_dir)
        self.timeout = timeout
        self.refresh = refresh
        self.cache_only = cache_only
        self.online_failed = False

    def neighbors(self, term):
        key = hashlib.sha256((self.knowledge + "\0" + term).encode()).hexdigest()
        snapshot_path = self.cache_dir / (key + ".json")
        if snapshot_path.exists() and not self.refresh:
            snapshot = read_json(snapshot_path)
            if snapshot["query"] != term:
                raise ValueError("ConceptNet cache query mismatch")
            return snapshot
        if self.cache_only:
            raise ValueError(f"Missing ConceptNet replay snapshot: {snapshot_path}")
        diagnostics = []
        edges = []
        matched = None
        backend = "none"
        source = None
        forms = concept_forms(term)
        for form in forms:
            if self.knowledge in ("auto", "online") and not self.online_failed:
                url = "https://api.conceptnet.io/c/en/" + quote(form.replace(" ", "_"), safe="") + "?limit=1000"
                try:
                    request = net.Request(url, headers={"User-Agent": USER_AGENT,
                                                        "Accept": "application/json"})
                    with net.urlopen(request, timeout=min(self.timeout, 8)) as response:
                        payload = json.loads(response.read())
                    if not isinstance(payload, dict) or not isinstance(payload.get("edges"), list):
                        raise ValueError("Unknown ConceptNet API schema")
                    for item in payload["edges"]:
                        su, ou = item["start"]["@id"], item["end"]["@id"]
                        s, o = concept(su, "en"), concept(ou, "en")
                        weight = float(item["weight"])
                        if (item["rel"]["@id"] in RELATIONS and s and o and s != o
                                and form in (s, o) and math.isfinite(weight) and weight >= 1):
                            edges.append({"assertion": item["@id"], "relation": item["rel"]["@id"],
                                          "subject": s, "object": o, "subject_uri": su, "object_uri": ou,
                                          "weight": weight, "metadata": item})
                    backend, source = "online", {"url": url, "view": payload.get("view"), "limit": 1000}
                except (net.URLError, OSError, ValueError, KeyError, TypeError) as error:
                    diagnostics.append(f"ConceptNet online unavailable: {error}")
                    self.online_failed = True
            if not edges and self.knowledge in ("auto", "local"):
                if self.index.exists():
                    with closing(sqlite3.connect(self.index.resolve().as_uri() + "?mode=ro", uri=True)) as db:
                        manifest = json.loads(db.execute(
                            "SELECT value FROM metadata WHERE key='manifest'").fetchone()[0])
                        # Separate indexed endpoint queries avoid a full-table OR scan.
                        records = []
                        for column in ("subject", "object"):
                            records += db.execute(
                                f"SELECT evidence FROM edges WHERE {column}=? "
                                "ORDER BY weight DESC, assertion LIMIT 1000", (form,)).fetchall()
                        edges = list({edge["assertion"]: edge for (raw,) in records
                                      for edge in [json.loads(raw)]}.values())
                    backend, source = "local", manifest
                else:
                    diagnostics.append(f"Local index missing: {self.index}; run build-index once")
            if edges:
                matched = form
                break
        snapshot = {"query": term, "matched_concept": matched, "forms_checked": forms,
                    "backend": backend, "source": source, "diagnostics": diagnostics,
                    "edges": sorted(edges, key=edge_order),
                    "scope": "up to 1000 edges per endpoint; permitted positive relations, English"}
        # Failed service calls are not permanent negative cache entries.
        if backend != "none":
            write_json(snapshot_path, snapshot)
        return snapshot
