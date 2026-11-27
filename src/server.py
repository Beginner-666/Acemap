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
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import config as ranking_config
from . import constants, graph_index
from .api_clients import (ApiConfigurationError, DeepSeekClient,
                           OpenAlexClient, build_deepseek_messages,
                           load_api_config, provider_status)
from .acemap_client import AcemapClient
from .conceptnet_provider import ConceptNet
from .jsonio import digest, read_json
from .query_understanding import parse_intent, select_expansions
from .rag import build_context, build_document, generate
from .rankers import get_ranker
from .search import _document_graph_version, search_one_v3
from .edges import rank_neighbors


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
    documents = {}
    rows = {}
    for key, filename in files.items():
        path = constants.ROOT / "results" / filename
        if not path.exists():
            continue
        document = read_json(path)
        documents[key] = document
        rows[key] = _find_row(document, query)
    if not rows.get("v3"):
        return None
    return {"schema_version": "api-response-v1", "source": "offline-snapshot",
            "query": query, "live": False,
            "v3": {"searches": [rows["v3"]]},
            "baseline": {"searches": [rows.get("baseline") or rows["v3"]]},
            "rrf": {"searches": [rows.get("rrf") or rows["v3"]]},
            "graphRank": {"searches": [rows.get("graphRank") or rows["v3"]]}}


def _live_search(query: str, ranker_name: str = "graph_rerank",
                 size: int = 10) -> dict:
    """Run one online query through the existing v3 pipeline."""
    query = " ".join(query.split())
    if not query:
        raise ValueError("query must be non-empty")
    if ranker_name not in {"baseline", "rrf", "graph_rerank"}:
        raise ValueError("unknown ranker")
    ranking = ranking_config.load_ranking_config(constants.ROOT / "config" / "ranking.json")
    resolved = ranking_config.resolve_for_ranker(ranking, ranker_name)
    ranker = get_ranker(ranker_name, config=resolved)
    timeout = 20.0
    provider = ConceptNet(
        knowledge="auto", index=constants.DEFAULT_INDEX,
        cache_dir=constants.DEFAULT_CONCEPTNET_CACHE, timeout=timeout)
    client = AcemapClient(constants.DEFAULT_ACEAMP_CACHE, timeout, 1, False, False)
    intent = parse_intent(query)
    search_term = (intent["topic"] or query) if ranker_name == "graph_rerank" else query
    snapshot = provider.neighbors(search_term)
    matched = snapshot.get("matched_concept")
    if ranker_name == "baseline":
        expansion = []
    elif matched and ranker_name == "graph_rerank":
        labels = {e["subject"] for e in snapshot.get("edges", [])}
        labels.update(e["object"] for e in snapshot.get("edges", []))
        degrees = graph_index.node_degrees(constants.DEFAULT_INDEX, labels)
        expansion = select_expansions(
            snapshot["edges"], matched, 3,
            min_edge_weight=resolved["graph"]["min_edge_weight"],
            degree_cap=resolved["graph"]["degree_cap"],
            relation_whitelist=resolved["graph"]["relation_whitelist"],
            degrees=degrees)
    elif matched:
        expansion = rank_neighbors(snapshot["edges"], matched, 3)
    else:
        expansion = []
    expansion = [x for x in expansion if x["term"] not in (query, search_term)]
    graph_context = {
        "graph_version": _document_graph_version(
            [{"conceptnet": snapshot}], local_graph_id=provider.graph_id),
        "edges": snapshot.get("edges", []), "expansions": expansion,
        "topic": intent.get("topic"), "time_filter": intent.get("time_filter"),
        "search_term": search_term,
    }
    row = search_one_v3(
        client, query, max(1, min(int(size), 40)), expansion, ranker,
        graph_context, search_term=search_term, baseline_only=ranker_name == "baseline")
    row["conceptnet"] = snapshot
    row["source"] = "live-api"
    response = {"schema_version": "api-response-v1", "source": "live-api",
                "query": query, "live": True}
    one = {"searches": [row]}
    response.update({"v3": one, "baseline": one, "rrf": one, "graphRank": one})
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
                query = values.get("q", values.get("query", [""]))[0]
                ranker = values.get("ranker", ["graph_rerank"])[0]
                response = _snapshot_response(query) or _live_search(query, ranker)
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
