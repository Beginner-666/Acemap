"""Focused regression checks; temporary fixtures are not submitted research data."""

import argparse
import contextlib
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch
import uuid

import acemap_search as app
import conceptnet_dynamic as dynamic
from urllib.error import URLError


@contextlib.contextmanager
def test_directory():
    # Windows sandbox ACLs do not permit access to mode-0700 mkdtemp folders.
    root = app.ROOT / "tests" / ("work_" + uuid.uuid4().hex)
    root.mkdir()
    try:
        yield root
    finally:
        for child in root.iterdir():
            child.unlink()
        root.rmdir()


class SearchTests(unittest.TestCase):
    def test_online_neighbors_preserve_original_edge(self):
        with test_directory() as root:
            args = argparse.Namespace(index=root / "missing.sqlite", conceptnet_cache=root,
                                      knowledge="online", refresh=False, cache_only=False, timeout=1)
            edge = {"@id": "/a/fixture", "start": {"@id": "/c/en/robotics"},
                    "end": {"@id": "/c/en/robot"}, "rel": {"@id": "/r/RelatedTo"}, "weight": 2}
            response = io.BytesIO(json.dumps({"edges": [edge]}).encode())
            with patch.object(dynamic, "urlopen", return_value=response):
                snapshot = dynamic.ConceptNet(args).neighbors("robotics")
            self.assertEqual(snapshot["backend"], "online")
            self.assertEqual(snapshot["edges"][0]["metadata"], edge)

    def test_dynamic_index_cross_domain_and_online_failure_replay(self):
        with test_directory() as root:
            source = root / "source.tsv"
            source.write_text(
                '/a/robot\t/r/RelatedTo\t/c/en/robotics\t/c/en/robot\t{"weight":2}\n'
                '/a/mineral\t/r/IsA\t/c/en/quartz\t/c/en/mineral\t{"weight":3}\n'
                '/a/ai\t/r/RelatedTo\t/c/en/artificial_intelligence\t/c/en/machine_learning\t{"weight":2}\n',
                encoding="utf-8")
            index = root / "index.sqlite"
            with contextlib.redirect_stdout(io.StringIO()):
                dynamic.build_index(source, index)
            args = argparse.Namespace(index=index, conceptnet_cache=root,
                                      knowledge="auto", refresh=False, cache_only=False, timeout=1)
            provider = dynamic.ConceptNet(args)
            with patch.object(dynamic, "urlopen", side_effect=URLError("fixture 502")) as network:
                robotics = provider.neighbors("robotics")
                mineral = provider.neighbors("mineral")
                ai = provider.neighbors("ai")
            self.assertEqual(network.call_count, 1)
            self.assertEqual(robotics["backend"], "local")
            self.assertEqual(robotics["edges"][0]["object"], "robot")
            self.assertEqual(mineral["edges"][0]["subject"], "quartz")
            self.assertEqual(ai["matched_concept"], "artificial intelligence")
            self.assertIsNone(provider.neighbors("embodied ai")["matched_concept"])
            args.cache_only = True
            with patch.object(dynamic, "urlopen", side_effect=AssertionError("network forbidden")):
                self.assertEqual(robotics, provider.neighbors("robotics"))
            with self.assertRaises(ValueError):
                provider.neighbors("never cached")

    def test_domain_support_precedes_ambiguous_high_weight(self):
        def edge(subject, obj, weight):
            return {"assertion": subject + ":" + obj, "subject": subject, "object": obj,
                    "subject_uri": "/c/en/" + subject, "object_uri": "/c/en/" + obj,
                    "relation": "/r/RelatedTo", "weight": weight}
        graph = {"extraction_config": {"seeds": ["rock", "mineral"]},
                 "edges": [edge("rock", "music", 9), edge("rock", "stone", 2),
                           edge("mineral", "stone", 1)]}
        result = app.expand(graph, "rock", 1)
        self.assertEqual(result[0]["term"], "stone")
        self.assertEqual(result[0]["domain_support_seeds"], ["mineral"])

    def test_elasticsearch_response_and_unknown_schema(self):
        self.assertEqual(app.extract_records({"hits": {"hits": [
            {"_id": "paper-1", "_source": {"title": "Rock"}}
        ]}}), [{"id": "paper-1", "title": "Rock"}])
        self.assertEqual(app.extract_records({"results": []}), [])
        with self.assertRaises(ValueError):
            app.extract_records({"unexpected": []})
        with self.assertRaises(ValueError):
            app.extract_records({"success": False, "results": []})

    def test_realistic_tsv_preserves_evidence(self):
        with test_directory() as directory:
            root = Path(directory)
            config = root / "config.json"
            app.write_json(config, {
                "domain": "fixture", "source_name": "test fixture",
                "source_url": "fixture", "language": "en", "seeds": ["rock"],
                "relations": ["/r/IsA"], "min_weight": 1, "edges_per_seed": 1,
            })
            source = root / "assertions.tsv"
            source.write_text("\n".join([
                '/a/low\t/r/IsA\t/c/en/rock\t/c/en/material\t{"weight":1}',
                '/a/high\t/r/IsA\t/c/en/granite\t/c/en/rock\t{"weight":3}',
                '/a/foreign\t/r/IsA\t/c/en/rock\t/c/fr/pierre\t{"weight":9}',
                'malformed',
            ]) + "\n", encoding="utf-8")
            output = root / "graph.json"
            with contextlib.redirect_stdout(io.StringIO()):
                app.build_graph(argparse.Namespace(config=config, assertions=source, output=output))
            graph = app.read_json(output)
            self.assertEqual(graph["statistics"]["edges"], 1)
            self.assertEqual(graph["source"]["sha256"], app.digest(source))
            expansion = app.expand(graph, "ROCK", 3)
            self.assertEqual(expansion[0]["term"], "granite")
            self.assertEqual(expansion[0]["evidence"]["source_line"], 2)
            self.assertEqual(app.expand(graph, "unknown", 3), [])

    def test_doi_and_duplicates_do_not_inflate_fusion(self):
        self.assertEqual(app.identity({"doi": "https://doi.org/10.1/ABC"}),
                         app.identity({"doi": "10.1/abc"}))
        class Client:
            def search(self, term, size):
                return [{"id": "one"}, {"id": "one"}], "fixture.json"
        result = app.search_one(argparse.Namespace(max_expansions=3, size=10),
                                {"edges": []}, Client(), "rock")
        self.assertEqual(result["metrics"]["enhanced_unique"], 1)
        self.assertAlmostEqual(result["results"][0]["fusion_score"], 1 / 61)
        self.assertFalse(result["enhancement_applied"])

    def test_missing_cache_never_calls_network(self):
        with test_directory() as directory:
            client = app.AcemapClient(Path(directory), 1, 0, True, False)
            with patch.object(client, "fetch", side_effect=AssertionError("network forbidden")):
                with self.assertRaises(ValueError):
                    client.search("rock", 10)

    def test_cached_response_replays_without_network(self):
        with test_directory() as directory:
            client = app.AcemapClient(Path(directory), 1, 0, False, False)
            with patch.object(client, "fetch", return_value=json.dumps({"results": [{"id": "one"}]})):
                original = client.search("rock", 10)
            client.cache_only = True
            with patch.object(client, "fetch", side_effect=AssertionError("network forbidden")):
                self.assertEqual(original, client.search("rock", 10))

    def test_failed_request_is_not_reported_as_success(self):
        class Client:
            def search(self, term, size):
                raise ValueError("fixture failure")
        result = app.search_one(argparse.Namespace(max_expansions=3, size=10),
                                {"edges": []}, Client(), "rock")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["metrics"]["failed_requests"], 1)


if __name__ == "__main__":
    unittest.main()
