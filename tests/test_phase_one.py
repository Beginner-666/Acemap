"""Phase-one tests: query understanding, graph rerank, and multi-hop expansion."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import math
import shutil
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from src import (config, constants, jsonio, multihop, rankers, search,
                 verification)
from src.conceptnet_provider import SNAPSHOT_SCOPE
from src.query_understanding import parse_intent, select_expansions
from src.versions import config_hash

MANIFEST = {
    "schema_version": 1,
    "source_name": "fixture assertions",
    "source_url": "https://example.invalid/assertions",
    "source_sha256": "a" * 64,
    "source_file": "fixture.tsv",
    "rows_scanned": 1,
    "edges_indexed": 1,
    "language": "en",
    "relations": ["/r/RelatedTo"],
    "min_weight": 1,
}

EDGE = {
    "assertion": "/a/1",
    "relation": "/r/RelatedTo",
    "subject": "rock",
    "object": "robot",
    "subject_uri": "/c/en/rock",
    "object_uri": "/c/en/robot",
    "weight": 2.0,
    "source_line": 1,
    "metadata": {"weight": 2},
}


@contextlib.contextmanager
def test_directory():
    root = constants.ROOT / "tests" / ("work_" + uuid.uuid4().hex)
    root.mkdir()
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


class FakeClient:
    def __init__(self):
        self.calls = []

    def search(self, term, size):
        self.calls.append((term, size))
        return {
            "rock": [
                {"id": "a", "title": "Rock mechanics", "publication_year": 2024,
                 "authorships": [{
                     "author": {"id": "a1", "display_name": "Alice"},
                     "institutions": [{"id": "i1", "display_name": "University"}],
                 }]},
                {"id": "b", "title": "Stone", "publication_year": 2000},
            ],
            "robot": [
                {"id": "c", "title": "Robot learning", "publication_year": 2023,
                 "authorships": [{
                     "author": {"id": "a2", "display_name": "Bob"},
                     "institutions": [{"id": "i2", "display_name": "Lab"}],
                 }]},
            ],
        }[term], "f.json"


class FakeHopClient:
    def __init__(self):
        self.calls = []

    def search_filtered(self, keyword, size, author_ids=None, institution_ids=None):
        self.calls.append((keyword, size, tuple(author_ids or ()),
                           tuple(institution_ids or ())))
        return [
            {"id": "d", "title": "New robot paper", "publication_year": 2025},
            {"id": "a", "title": "Rock mechanics"},
        ], "g.json"


def graph_context(time_filter=None):
    expansion = [{"term": "robot", "evidence": EDGE, "domain_support_seeds": []}]
    return {"edges": [EDGE], "expansions": expansion, "topic": "rock",
            "time_filter": time_filter}, expansion


def v3_row_and_settings(time_filter=None):
    resolved = config.resolve_for_ranker(config.default_ranking_config(), "graph_rerank")
    ranker = rankers.get_ranker("graph_rerank", config=resolved)
    context, expansion = graph_context(time_filter)
    row = search.search_one_v3(FakeClient(), "rock", 10, expansion, ranker, context)
    row["conceptnet"] = {
        "query": "rock", "matched_concept": "rock", "forms_checked": ["rock"],
        "backend": "local", "source": dict(MANIFEST), "diagnostics": [],
        "edges": [dict(EDGE)], "scope": SNAPSHOT_SCOPE,
    }
    row["warning"] = None
    settings = {
        "knowledge": "auto", "size_per_term": 10, "max_expansions": 3,
        "lexical_forms": "ai -> artificial intelligence; exact phrases only",
        "fusion": resolved["fusion"], "ranker": "graph_rerank",
        "ranker_version": "graph-rerank-v1", "ranker_config": resolved,
        "config_hash": config_hash(resolved), "graph_version": "g-" + "0" * 16,
        "snapshot_key_schema": 2, "record_feature_version": 1,
    }
    document = {
        "schema_version": 3, "domain": "query-time English ConceptNet",
        "endpoint": constants.ENDPOINT, "settings": settings,
        "summary": {"queries": 1, "successful_queries": 1,
                    "successful_with_enhancement": 1},
        "searches": [row],
    }
    return row, document


class QueryUnderstandingTests(unittest.TestCase):
    def test_intent_parsing_and_fallback(self):
        intent = parse_intent("recent research on plate tectonics")
        self.assertEqual(intent["topic"], "plate tectonics")
        self.assertEqual(intent["time_filter"], "recent")
        self.assertEqual(intent["task"], "research")
        self.assertTrue(intent["parsed"])
        year = parse_intent("papers 2019-2023 on robotics")
        self.assertEqual(year["time_filter"], {"start": 2019, "end": 2023})
        self.assertEqual(year["topic"], "robotics")
        fallback = parse_intent("plate tectonics")
        self.assertEqual(fallback["topic"], "plate tectonics")
        self.assertIsNone(fallback["time_filter"])
        self.assertFalse(fallback["parsed"])

    def test_select_expansions_filters_and_morphology(self):
        def edge(relation, subject, obj, weight):
            return {"assertion": subject + ":" + obj + relation,
                    "relation": relation, "subject": subject, "object": obj,
                    "subject_uri": "/c/en/" + subject,
                    "object_uri": "/c/en/" + obj, "weight": weight}

        edge_list = [
            edge("/r/RelatedTo", "robot", "robotics", 1),
            edge("/r/RelatedTo", "robot", "x", 9),
            edge("/r/RelatedTo", "robot", "thing", 9),
            edge("/r/RelatedTo", "robot", "low", 0.5),
            edge("/r/Synonym", "robot", "robotic", 1),
            edge("/r/IsA", "robot", "automaton", 3),
            edge("/r/RelatedTo", "robot", "robots", 9),
        ]
        selected = select_expansions(edge_list, "robot", 5, min_edge_weight=1)
        self.assertEqual([item["term"] for item in selected],
                         ["automaton", "robotic", "robotics"])
        self.assertNotIn("x", [item["term"] for item in selected])
        self.assertNotIn("thing", [item["term"] for item in selected])
        self.assertNotIn("robots", [item["term"] for item in selected])


class GraphRerankTests(unittest.TestCase):
    def test_components_score_and_traceable_paths(self):
        row, document = v3_row_and_settings(time_filter="recent")
        verification.validate_v3_document(document)
        results = row["results"]
        self.assertEqual([item["key"] for item in results], ["id:c", "id:a", "id:b"])
        robot = next(item for item in results if item["key"] == "id:c")
        self.assertEqual(robot["score_components"]["graph"], 1.0)
        self.assertEqual(robot["evidence"]["graph_paths"][0]["assertions"], ["/a/1"])
        self.assertGreater(robot["score_components"]["freshness"], 0.0)
        weights = document["settings"]["ranker_config"]["weights"]
        expected = sum(weights[key] * robot["score_components"][key]
                       for key in verification.COMPONENT_KEYS)
        self.assertTrue(math.isclose(robot["score"], expected, rel_tol=0, abs_tol=1e-12))
        old = next(item for item in results if item["key"] == "id:b")
        self.assertEqual(old["score_components"]["graph"], 0.0)
        self.assertGreater(robot["score_components"]["freshness"],
                           old["score_components"]["freshness"])

    def test_no_graph_context_degrades_to_zero(self):
        resolved = config.resolve_for_ranker(config.default_ranking_config(), "graph_rerank")
        ranker = rankers.get_ranker("graph_rerank", config=resolved)
        term_results = [{"term": "rock", "is_original": True, "status": "ok",
                         "records": [{"id": "a", "title": "Rock"}],
                         "cache_file": "f.json", "error": None}]
        output = ranker.rank("rock", term_results, None)
        self.assertEqual(output["results"][0]["score_components"]["graph"], 0)
        self.assertEqual(output["results"][0]["evidence"]["graph_paths"], [])

    def test_freshness_disabled_without_recent_intent(self):
        row, _ = v3_row_and_settings(time_filter=None)
        self.assertTrue(all(item["score_components"]["freshness"] == 0.0
                            for item in row["results"]))


class SearchPipelineTests(unittest.TestCase):
    def test_v3_pipeline_shape_and_metrics(self):
        row, document = v3_row_and_settings()
        verification.validate_v3_document(document)
        self.assertEqual(row["status"], "ok")
        self.assertEqual(row["metrics"], {"baseline_unique": 2, "enhanced_unique": 3,
                                          "additional_unique": 1, "failed_requests": 0})
        first = row["results"][0]
        self.assertEqual(list(first), ["key", "paper", "score", "score_components",
                                       "evidence", "matches"])
        self.assertTrue(first["score"] >= row["results"][-1]["score"])

    def test_rrf_ranker_replays_v2_order(self):
        term_results = [
            {"term": "rock", "is_original": True, "status": "ok",
             "records": [{"id": "a"}, {"id": "b"}], "cache_file": "f.json",
             "error": None},
            {"term": "robot", "is_original": False, "status": "ok",
             "records": [{"id": "b"}, {"id": "c"}], "cache_file": "g.json",
             "error": None},
        ]
        ranked, _ = rankers.rrf_merge("rock", term_results)
        output = rankers.RRFRanker().rank("rock", term_results)
        self.assertEqual([item["key"] for item in output["results"]],
                         [item["key"] for item in ranked])


class MultihopTests(unittest.TestCase):
    def test_one_hop_merges_base_and_records_paths(self):
        row, _ = v3_row_and_settings()
        output = multihop.expand_query(row, FakeHopClient(), multihop.load_config())
        self.assertEqual(output["status"], "ok")
        self.assertEqual(len(output["seeds"]), 4)
        keys = [item["key"] for item in output["results"]]
        self.assertIn("id:a", keys)
        self.assertIn("id:d", keys)
        new = next(item for item in output["results"] if item["key"] == "id:d")
        self.assertGreater(new["multihop_score"], 0.0)
        self.assertLess(new["score"], min(item["score"] for item in row["results"]))
        self.assertTrue(new["sources"])
        path = new["sources"][0]
        self.assertEqual(path["query"], "rock")
        self.assertIn(path["node"]["type"], ("author", "institution"))
        self.assertEqual(path["paper_key"], "id:d")
        existing = next(item for item in output["results"] if item["key"] == "id:a")
        self.assertTrue(existing["sources"])
        self.assertTrue(all(item["multihop_score"] < item["base_score"]
                            for item in output["results"] if item["base_score"] > 0))

    def test_invalid_hop_weight_is_rejected(self):
        config = multihop.load_config()
        config["hop_weight"] = True
        with self.assertRaises(ValueError):
            multihop.validate_config(config)

    def test_threshold_skips_but_keeps_scored_base_results(self):
        row, _ = v3_row_and_settings()
        config = multihop.load_config()
        config["min_seed_score"] = 2.0
        output = multihop.expand_query(row, FakeHopClient(), config)
        self.assertEqual(output["status"], "skipped")
        self.assertFalse(output["trigger"]["applied"])
        self.assertEqual(output["requests"], [])
        self.assertTrue(output["results"])
        self.assertTrue(all(item["score"] == item["base_score"]
                            and item["first_round"] for item in output["results"]))

    def test_failure_keeps_base_results(self):
        row, _ = v3_row_and_settings()

        class Broken:
            def search_filtered(self, *args, **kwargs):
                raise ValueError("fixture failure")

        output = multihop.expand_query(row, Broken(), multihop.load_config())
        self.assertEqual(output["status"], "failed")
        self.assertEqual(len(output["results"]), len(row["results"]))
        self.assertTrue(all(item["multihop_score"] == 0 for item in output["results"]))

    def test_run_multihop_validates_and_replays(self):
        row, document = v3_row_and_settings()
        with test_directory() as root:
            source = root / "v3.json"
            output = root / "multihop.json"
            jsonio.write_json(source, document)
            args = argparse.Namespace(
                results=source, output=output,
                config=constants.ROOT / "config" / "multihop.json",
                cache=root, timeout=1, retries=0, cache_only=False, refresh=False)
            with patch("src.multihop.AcemapClient", return_value=FakeHopClient()):
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(multihop.run_multihop(args), 0)
            replay = jsonio.read_json(output)
            multihop.validate_document(replay)
            self.assertEqual(replay["source"]["sha256"], jsonio.digest(source))
            self.assertEqual(replay["summary"]["additional_papers"], 1)


class FilteredClientTests(unittest.TestCase):
    def test_filtered_search_caches_request_and_replays_offline(self):
        from src.acemap_client import AcemapClient

        with test_directory() as root:
            client = AcemapClient(root, 1, 0, False, False)
            with patch.object(client, "fetch",
                              return_value=json.dumps({"results": [{"id": "one"}]})):
                records, cache_name = client.search_filtered(
                    "robotics", 5, author_ids=["b", "a"])
            self.assertEqual(records, [{"id": "one"}])
            snapshot = jsonio.read_json(root / cache_name)
            self.assertEqual(snapshot["parameters"]["author_ids"], "a,b")
            client.cache_only = True
            with patch.object(client, "fetch", side_effect=AssertionError("network")):
                self.assertEqual(client.search_filtered(
                    "robotics", 5, author_ids=["a", "b"]), ([{"id": "one"}], cache_name))


class CliTests(unittest.TestCase):
    def test_v3_and_multihop_commands_parse(self):
        from src.cli import build_parser

        parser = build_parser()
        args = parser.parse_args(["search", "--query", "rock",
                                  "--schema-version", "3",
                                  "--ranker", "graph_rerank"])
        self.assertEqual(args.schema_version, 3)
        self.assertEqual(args.ranker, "graph_rerank")
        hop = parser.parse_args(["multihop"])
        self.assertEqual(hop.command, "multihop")
        from src.cli import main
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                main(["search", "--query", "rock", "--ranker", "rrf"])


if __name__ == "__main__":
    unittest.main()
