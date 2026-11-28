"""Local HTTP gateway for the ScholarLink visualization.

The gateway keeps provider credentials on the Python side and serves the
static demo from the same origin.  It is intentionally dependency free so the
project can run with a stock Python 3.10+ installation.
"""

from __future__ import annotations

import json
import os
import re
from functools import partial
from concurrent.futures import ThreadPoolExecutor
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import config as ranking_config
from . import constants, graph_index
from .api_clients import (ApiConfigurationError, DeepSeekClient,
                           OpenAlexClient, build_deepseek_messages,
                           load_api_config, provider_status)
from .acemap_client import AcemapClient
from .conceptnet_provider import ConceptNet, SNAPSHOT_SCOPE
from .jsonio import digest, read_json
from .records import identity
from .query_understanding import parse_intent, select_expansions
from .rag import build_context, build_document, generate
from .rankers import get_ranker
from .search import _collect_term_results, _document_graph_version


def _config_path() -> Path:
    configured = os.environ.get("SCHOLARLINK_API_CONFIG")
    if configured:
        return Path(configured)
    local = constants.ROOT / "config" / "api.local.json"
    return local if local.exists() else constants.ROOT / "config" / "api.example.json"


def _api_config() -> dict:
    return load_api_config(_config_path())


def _load_document(name: str) -> dict:
    return read_json(constants.ROOT / "results" / name)


def _find_row(document: dict, query: str) -> dict | None:
    query = " ".join(str(query).split()).casefold()
    for row in document.get("searches", []):
        if " ".join(str(row.get("query", "")).split()).casefold() == query:
            return row
    return None


def _snapshot_response(query: str) -> dict | None:
    """Return all ranker snapshots when the query is already frozen locally."""
    files = {
        "v3": "dynamic_queries_v3.json",
        "baseline": "eval_baseline.json",
        "rrf": "eval_rrf.json",
        "graphRank": "eval_graph_rerank.json",
    }
    rows = {}
    for key, filename in files.items():
        path = constants.ROOT / "results" / filename
        if not path.exists():
            continue
        document = read_json(path)
        rows[key] = _find_row(document, query)
    representative = rows.get("v3") or rows.get("graphRank") or rows.get("baseline")
    if not representative:
        return None
    return {"schema_version": "api-response-v1", "source": "offline-snapshot",
            "query": query, "live": False,
            "v3": {"searches": [representative]},
            "baseline": {"searches": [rows.get("baseline") or representative]},
            "rrf": {"searches": [rows.get("rrf") or representative]},
            "graphRank": {"searches": [rows.get("graphRank") or representative]}}


def _live_search(query: str, size: int = 25, retrieval_query: str = "", focus: list[str] | None = None) -> dict:
    """Retrieve online once and compute all three rankings from the candidates."""
    query = " ".join(query.split())
    ranking = ranking_config.load_ranking_config(constants.ROOT / "config" / "ranking.json")
    resolved = ranking_config.resolve_for_ranker(ranking, "graph_rerank")
    provider = ConceptNet(
        knowledge="auto", index=constants.DEFAULT_INDEX,
        cache_dir=constants.DEFAULT_CONCEPTNET_CACHE, timeout=4)
    client = AcemapClient(constants.DEFAULT_ACEAMP_CACHE, 8, 0, False, True, persist=False)
    intent = parse_intent(retrieval_query or query)
    search_term = intent["topic"] or retrieval_query or query
    size = max(1, min(int(size), 40))
    originals, requests = _collect_term_results(client, query, size, [], search_term=search_term)
    if originals[0]["status"] == "failed":
        raise ValueError("Acemap live search failed")
    focused_terms = [f"{search_term} {topic}" for topic in (focus or [])]
    focus_requests = []
    if focused_terms:
        def fetch_focus(term):
            return _collect_term_results(client, query, size, [], search_term=term)

        with ThreadPoolExecutor(max_workers=3) as pool:
            focused_results = list(pool.map(fetch_focus, focused_terms))
        seen = {identity(record) for record in originals[0]["records"]}
        for focused, calls in focused_results:
            focus_requests.extend(calls)
            for record in focused[0]["records"]:
                key = identity(record)
                if key not in seen:
                    originals[0]["records"].append(record)
                    seen.add(key)
    requests.extend(focus_requests)
    try:
        snapshot = provider.neighbors(search_term)
    except (OSError, ValueError) as exc:
        snapshot = {"query": search_term, "matched_concept": None, "backend": "none",
                    "source": None, "edges": [], "scope": SNAPSHOT_SCOPE,
                    "diagnostics": [str(exc)]}
    matched = snapshot.get("matched_concept")
    expansion = []
    if matched:
        labels = {e["subject"] for e in snapshot.get("edges", [])}
        labels.update(e["object"] for e in snapshot.get("edges", []))
        degrees = graph_index.node_degrees(constants.DEFAULT_INDEX, labels)
        expansion = select_expansions(
            snapshot["edges"], matched, 3,
            min_edge_weight=resolved["graph"]["min_edge_weight"],
            degree_cap=resolved["graph"]["degree_cap"],
            relation_whitelist=resolved["graph"]["relation_whitelist"],
            degrees=degrees)
    expansion = [x for x in expansion if x["term"] not in (query, search_term)]
    graph_context = {
        "graph_version": _document_graph_version(
            [{"conceptnet": snapshot}], local_graph_id=provider.graph_id),
        "edges": snapshot.get("edges", []), "expansions": expansion,
        "topic": intent.get("topic"), "time_filter": intent.get("time_filter"),
        "search_term": search_term,
    }
    # Fetch just the expansion terms, then mark them as secondary inputs.
    term_results = list(originals)
    if expansion:
        extra, extra_requests = _collect_term_results(
            client, query, size, expansion[1:], search_term=expansion[0]["term"])
        for entry in extra:
            entry["is_original"] = False
        term_results.extend(extra)
        requests.extend(extra_requests)
    response = {"schema_version": "api-response-v1", "source": "live-api",
                "query": query, "live": True, "provider": "Acemap",
                "search_term": search_term, "profile_focus": focus or []}
    for key, name in (("baseline", "baseline"), ("rrf", "rrf"), ("graphRank", "graph_rerank")):
        config = ranking_config.resolve_for_ranker(ranking, name)
        ranker = get_ranker(name, config=config)
        baseline = name == "baseline"
        ranked = ranker.rank(query, originals if baseline else term_results, graph_context)
        row = {"query": query, "source": "live-api", "conceptnet": snapshot,
               "status": "partial" if any(call["status"] == "failed" for call in requests) else "ok",
               "expansions": [] if baseline else expansion,
               "requests": requests[:1 + len(focus_requests)] if baseline else requests, **ranked}
        response[key] = {"searches": [row]}
    response["v3"] = response["graphRank"]
    return response


def _extract_llm_json(value: str) -> dict:
    value = value.strip()
    if value.startswith("```"):
        value = re.sub(r"^```(?:json)?\s*|\s*```$", "", value,
                       flags=re.IGNORECASE | re.DOTALL).strip()
    parsed = json.loads(value)
    if not isinstance(parsed, dict) or not isinstance(parsed.get("claims"), list):
        raise ValueError("DeepSeek response must contain a claims array")
    return parsed


def _rag_response(body: dict) -> dict:
    query = str(body.get("query", "")).strip()
    if not query:
        raise ValueError("query is required")
    search = body.get("search")
    search_document = {"searches": [search]} if isinstance(search, dict) else None
    context = build_context(
        query, search_document=search_document,
        graph_facts=body.get("graph_facts"), papers=body.get("papers"),
        max_facts=min(int(body.get("max_facts", 12)), 30),
        max_papers=min(int(body.get("max_papers", 8)), 20))
    config = _api_config()
    answer_mode = body.get("answer_mode", "extractive")
    generator = None
    if answer_mode == "llm":
        status = provider_status(config).get("deepseek", {})
        if status.get("configured"):
            client = DeepSeekClient(config)

            def generator(ctx):
                raw = client.complete(build_deepseek_messages(query, ctx))
                content = raw["choices"][0]["message"]["content"]
                return _extract_llm_json(content)
        else:
            answer_mode = "extractive"
    generated = generate(context, generator=generator)
    document = build_document(context, generated, source={"provider": answer_mode})
    document["requested_answer_mode"] = body.get("answer_mode", "extractive")
    return document


class Handler(SimpleHTTPRequestHandler):
    server_version = "ScholarLink/1.0"

    def _send_json(self, status: int, payload: dict):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/api/health":
                return self._send_json(200, {"ok": True, "service": "scholarlink"})
            if parsed.path == "/api/status":
                config = _api_config()
                return self._send_json(200, {"ok": True, "config": _config_path().name,
                                             "providers": provider_status(config)})
            if parsed.path == "/api/search":
                values = parse_qs(parsed.query)
                query = " ".join(values.get("q", values.get("query", [""]))[0].split())
                term = " ".join(values.get("term", [""])[0].split())
                focus = list(dict.fromkeys(values.get("focus", [])))[:7]
                if not query or len(query) > 300 or len(term) > 300 or any(len(item) > 100 for item in focus):
                    return self._send_json(400, {"ok": False, "error": "Query must contain 1–300 characters"})
                live_first = values.get("live") == ["1"]
                response = None if live_first else _snapshot_response(query)
                if response is None:
                    try:
                        response = _live_search(query, retrieval_query=term, focus=focus)
                    except (ValueError, OSError):
                        response = _snapshot_response(query) or (term and _snapshot_response(term))
                        if not response:
                            return self._send_json(502, {"ok": False, "error": "Acemap live search unavailable"})
                        response["fallback_reason"] = "acemap-unavailable"
                return self._send_json(200, response)
            if parsed.path.startswith("/api/works/"):
                config = _api_config()
                return self._send_json(200, OpenAlexClient(config).work(parsed.path.rsplit("/", 1)[-1]))
            if parsed.path.startswith("/api/graph/"):
                term = parsed.path.rsplit("/", 1)[-1].replace("_", " ")
                provider = ConceptNet("auto", constants.DEFAULT_INDEX,
                                      constants.DEFAULT_CONCEPTNET_CACHE, 20)
                return self._send_json(200, provider.neighbors(term))
            return super().do_GET()
        except (ApiConfigurationError, ValueError, OSError, KeyError, TypeError) as exc:
            self._send_json(502, {"ok": False, "error": str(exc)})

    def do_POST(self):
        parsed = urlparse(self.path)
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
            if parsed.path == "/api/rag":
                return self._send_json(200, _rag_response(body))
            self._send_json(404, {"ok": False, "error": "unknown endpoint"})
        except (ApiConfigurationError, ValueError, OSError, KeyError, TypeError,
                json.JSONDecodeError) as exc:
            self._send_json(502, {"ok": False, "error": str(exc)})

    def log_message(self, fmt, *args):
        print("[scholarlink] " + fmt % args, flush=True)


def serve(host: str = "127.0.0.1", port: int = 8000):
    handler = partial(Handler, directory=str(constants.ROOT))
    server = ThreadingHTTPServer((host, port), handler)
    print(f"ScholarLink server: http://{host}:{port}/docs/visualization/", flush=True)
    print("API: /api/health /api/status /api/search /api/rag", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Stopping ScholarLink server", flush=True)
    finally:
        server.server_close()
