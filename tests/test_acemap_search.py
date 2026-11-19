"""Focused regression checks; temporary fixtures are not submitted research data."""

from contextlib import closing
import contextlib
import io
import json
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch
import uuid
from urllib.error import URLError

from src import (acemap_client, cli, conceptnet_index, conceptnet_provider,
                 constants, edges, jsonio, records, search)


@contextlib.contextmanager
def test_directory():
    # Windows sandbox ACLs do not permit access to mode-0700 mkdtemp folders.
    root = constants.ROOT / "tests" / ("work_" + uuid.uuid4().hex)
    root.mkdir()
    try:
        yield root
    finally:
        for child in root.iterdir():
            child.unlink()
        root.rmdir()


class SearchTests(unittest.TestCase):
    def test_json_output_uses_portable_lf_newlines(self):
        with test_directory() as root:
            target = root / "snapshot.json"
            jsonio.write_json(target, {"value": "first\nsecond"})
            raw = target.read_bytes()
            self.assertNotIn(b"\r\n", raw)
            self.assertTrue(raw.endswith(b"\n"))
            self.assertEqual(jsonio.read_json(target), {"value": "first\nsecond"})

    def test_online_neighbors_preserve_original_edge(self):
        with test_directory() as root:
            provider = conceptnet_provider.ConceptNet(
                knowledge="online", index=root / "missing.sqlite",
                cache_dir=root, timeout=1)
            edge = {"@id": "/a/fixture", "start": {"@id": "/c/en/robotics"},
                    "end": {"@id": "/c/en/robot"}, "rel": {"@id": "/r/RelatedTo"},
                    "weight": 2}
            response = io.BytesIO(json.dumps({"edges": [edge]}).encode())
            with patch("src.net.urlopen", return_value=response):
                snapshot = provider.neighbors("robotics")
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
                conceptnet_index.build_index(source, index)
            provider = conceptnet_provider.ConceptNet(
                knowledge="auto", index=index, cache_dir=root, timeout=1)
            with patch("src.net.urlopen", side_effect=URLError("fixture 502")) as network:
                robotics = provider.neighbors("robotics")
                mineral = provider.neighbors("mineral")
                ai = provider.neighbors("ai")
            self.assertEqual(network.call_count, 1)
            self.assertEqual(robotics["backend"], "local")
            self.assertEqual(robotics["edges"][0]["object"], "robot")
            self.assertEqual(mineral["edges"][0]["subject"], "quartz")
            self.assertEqual(ai["matched_concept"], "artificial intelligence")
            self.assertIsNone(provider.neighbors("embodied ai")["matched_concept"])
            provider.cache_only = True
            with patch("src.net.urlopen", side_effect=AssertionError("network forbidden")):
                self.assertEqual(robotics, provider.neighbors("robotics"))
            with self.assertRaises(ValueError):
                provider.neighbors("never cached")

    def test_neighbor_ranking_prefers_weight_and_keeps_evidence(self):
        def edge(subject, obj, weight):
            return {"assertion": subject + ":" + obj, "subject": subject, "object": obj,
                    "subject_uri": "/c/en/" + subject, "object_uri": "/c/en/" + obj,
                    "relation": "/r/RelatedTo", "weight": weight}

        ranked = edges.rank_neighbors(
            [edge("rock", "music", 9), edge("rock", "stone", 2), edge("stone", "rock", 1)],
            "rock", 2)
        self.assertEqual([item["term"] for item in ranked], ["music", "stone"])
        self.assertEqual(ranked[0]["evidence"]["weight"], 9)
        self.assertEqual(ranked[1]["evidence"]["subject"], "rock")
        self.assertEqual(ranked[1]["domain_support_seeds"], [])
        self.assertEqual(edges.rank_neighbors([edge("rock", "stone", 2)], "unknown", 3), [])

    def test_elasticsearch_response_and_unknown_schema(self):
        self.assertEqual(records.extract_records({"hits": {"hits": [
            {"_id": "paper-1", "_source": {"title": "Rock"}}
        ]}}), [{"id": "paper-1", "title": "Rock"}])
        self.assertEqual(records.extract_records({"results": []}), [])
        with self.assertRaises(ValueError):
            records.extract_records({"unexpected": []})
        with self.assertRaises(ValueError):
            records.extract_records({"success": False, "results": []})

    def test_index_keeps_normalized_labels_and_source_line(self):
        with test_directory() as directory:
            root = Path(directory)
            source = root / "assertions.tsv"
            source.write_text("\n".join([
                '/a/good\t/r/IsA\t/c/en/rock/n/wn/object\t/c/en/granite/n/wn/substance\t{"weight":3}',
                '/a/foreign\t/r/IsA\t/c/en/rock\t/c/fr/pierre\t{"weight":9}',
                '/a/low\t/r/IsA\t/c/en/rock\t/c/en/stone\t{"weight":0}',
                '/a/relation\t/r/Antonym\t/c/en/rock\t/c/en/stone\t{"weight":9}',
                'malformed',
            ]) + "\n", encoding="utf-8")
            index = root / "index.sqlite"
            with contextlib.redirect_stdout(io.StringIO()):
                conceptnet_index.build_index(source, index)
            with closing(sqlite3.connect(index)) as db:
                row = db.execute(
                    "SELECT assertion, subject, object, weight, evidence FROM edges").fetchone()
                manifest = json.loads(db.execute(
                    "SELECT value FROM metadata WHERE key='manifest'").fetchone()[0])
            self.assertEqual(row[:4], ("/a/good", "rock", "granite", 3.0))
            self.assertEqual(json.loads(row[4])["source_line"], 1)
            self.assertEqual(manifest["rows_scanned"], 5)
            self.assertEqual(manifest["edges_indexed"], 1)

    def test_doi_and_duplicates_do_not_inflate_fusion(self):
        self.assertEqual(records.identity({"doi": "https://doi.org/10.1/ABC"}),
                         records.identity({"doi": "10.1/abc"}))

        class Client:
            def search(self, term, size):
                return [{"id": "one"}, {"id": "one"}], "fixture.json"

        result = search.search_one(Client(), "rock", 10)
        self.assertEqual(result["metrics"]["enhanced_unique"], 1)
        self.assertAlmostEqual(result["results"][0]["fusion_score"], 1 / 61)
        self.assertFalse(result["enhancement_applied"])

    def test_missing_cache_never_calls_network(self):
        with test_directory() as directory:
            client = acemap_client.AcemapClient(Path(directory), 1, 0, True, False)
            with patch.object(client, "fetch", side_effect=AssertionError("network forbidden")):
                with self.assertRaises(ValueError):
                    client.search("rock", 10)

    def test_cached_response_replays_without_network(self):
        with test_directory() as directory:
            client = acemap_client.AcemapClient(Path(directory), 1, 0, False, False)
            with patch.object(client, "fetch",
                              return_value=json.dumps({"results": [{"id": "one"}]})):
                original = client.search("rock", 10)
            client.cache_only = True
            with patch.object(client, "fetch", side_effect=AssertionError("network forbidden")):
                self.assertEqual(original, client.search("rock", 10))

    def test_failed_request_is_not_reported_as_success(self):
        class Client:
            def search(self, term, size):
                raise ValueError("fixture failure")

        result = search.search_one(Client(), "rock", 10)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["metrics"]["failed_requests"], 1)

    def test_cli_exposes_only_dynamic_graph_commands(self):
        parser = cli.build_parser()
        args = parser.parse_args(["search", "--query", "rock"])
        self.assertEqual(args.knowledge, "auto")
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                parser.parse_args(["build-graph", "--assertions", "a",
                                   "--config", "b", "--output", "c"])
            with self.assertRaises(SystemExit):
                parser.parse_args(["search", "--query", "rock", "--knowledge", "graph"])


if __name__ == "__main__":
    unittest.main()
