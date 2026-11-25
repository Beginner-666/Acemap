"""Query-time ConceptNet neighbors with online and offline backends."""

from __future__ import annotations

import hashlib
import json
import math
from contextlib import closing
from pathlib import Path
import sqlite3
from urllib.parse import quote

from . import graph_index, net
from .conceptnet_index import RELATIONS
from .constants import USER_AGENT
from .edges import edge_order
from .jsonio import read_json, write_json
from .text import concept, normalize
from .versions import (CONCEPTNET_ONLINE_SOURCE_ID, MISSING_GRAPH_ID,
                       SNAPSHOT_KEY_SCHEMA)

SNAPSHOT_SCOPE = "up to 1000 edges per endpoint; permitted positive relations, English"


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
        self._graph_id = None

    def _load_graph_id(self):
        """Derive the local graph id lazily; online modes never need the index."""
        if self._graph_id is None:
            if not self.index.exists():
                self._graph_id = MISSING_GRAPH_ID
            else:
                try:
                    with closing(sqlite3.connect(
                            self.index.resolve().as_uri() + "?mode=ro", uri=True)) as db:
                        self._graph_id = graph_index.read_graph_id(db)
                except (sqlite3.Error, ValueError, KeyError, OSError):
                    # Cache lookup keeps working; the local read path will
                    # surface the concrete index error if it is actually used.
                    self._graph_id = MISSING_GRAPH_ID
        return self._graph_id

    @property
    def graph_id(self):
        """Return the resolved local graph id without changing snapshot bytes.

        The provider keeps the graph version in the cache key and attribute,
        never in the snapshot payload (see interface protocol §6.2).
        """
        return self._load_graph_id()

    def source_id(self, backend):
        """Return the cache source identifier for a successful backend."""
        if backend == "online":
            return CONCEPTNET_ONLINE_SOURCE_ID
        return "local:" + self._load_graph_id()

    def _cache_key(self, term, source_id):
        return hashlib.sha256(
            f"v{SNAPSHOT_KEY_SCHEMA}\0{self.knowledge}\0{source_id}\0{term}".encode()
        ).hexdigest()

    def _snapshot_candidates(self, term):
        """Return lookup candidates in priority order (online before local)."""
        source_ids = []
        if self.knowledge in ("auto", "online"):
            source_ids.append(CONCEPTNET_ONLINE_SOURCE_ID)
        if self.knowledge in ("auto", "local"):
            source_ids.append("local:" + self._load_graph_id())
        return [(source_id, self.cache_dir / (self._cache_key(term, source_id) + ".json"))
                for source_id in source_ids]

    def neighbors(self, term):
        candidates = self._snapshot_candidates(term)
        if not self.refresh:
            for _, snapshot_path in candidates:
                if snapshot_path.exists():
                    snapshot = read_json(snapshot_path)
                    if snapshot["query"] != term:
                        raise ValueError("ConceptNet cache query mismatch")
                    return snapshot
        if self.cache_only:
            raise ValueError(f"Missing ConceptNet replay snapshot: {candidates[0][1]}")
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
                    with closing(sqlite3.connect(
                            self.index.resolve().as_uri() + "?mode=ro", uri=True)) as db:
                        manifest = graph_index.read_manifest(db)
                        edges = graph_index.edges_for_term(db, form, 1000)
                    backend, source = "local", manifest
                else:
                    diagnostics.append(f"Local index missing: {self.index}; run build-index once")
            if edges:
                matched = form
                break
        snapshot = {"query": term, "matched_concept": matched, "forms_checked": forms,
                    "backend": backend, "source": source, "diagnostics": diagnostics,
                    "edges": sorted(edges, key=edge_order),
                    "scope": SNAPSHOT_SCOPE}
        # Failed service calls are not permanent negative cache entries.
        if backend != "none":
            source_id = self.source_id(backend)
            snapshot_path = self.cache_dir / (self._cache_key(term, source_id) + ".json")
            write_json(snapshot_path, snapshot)
        return snapshot
