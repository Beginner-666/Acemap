"""Phase-two tests for clean-v1 graph construction and extension ingestion."""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import shutil
import sqlite3
import unittest
import uuid
from pathlib import Path

from src import (conceptnet_index, conceptnet_provider, constants,
                 graph_clean, graph_index, search)


@contextlib.contextmanager
def test_directory():
    root = constants.ROOT / "tests" / ("work_" + uuid.uuid4().hex)
    root.mkdir()
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


def write_assertions(root, rows, name="assertions.tsv"):
    path = root / name
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return path


def build_raw(root, rows, name="raw.sqlite"):
    source = write_assertions(root, rows, name="raw_source.tsv")
    target = root / name
    with contextlib.redirect_stdout(io.StringIO()):
        conceptnet_index.build_index(source, target)
    return target, source


class CleaningRuleTests(unittest.TestCase):
    def test_clean_rules_manifest_and_evidence(self):
        with test_directory() as root:
            source = write_assertions(root, [
                '/a/good\t/r/IsA\t/c/en/rock/n/wn/object\t/c/en/granite/n/wn/substance\t{"weight":3,"dataset":"/d/x","license":"cc:by-sa/4.0"}',
                '/a/dup\t/r/IsA\t/c/en/rock\t/c/en/granite\t{"weight":2,"dataset":"/d/y","license":"cc:by-sa/4.0"}',
                '/a/self\t/r/RelatedTo\t/c/en/rock\t/c/en/rock\t{"weight":9}',
                '/a/fr\t/r/IsA\t/c/en/rock\t/c/fr/pierre\t{"weight":9}',
                '/a/badrel\t/r/Antonym\t/c/en/rock\t/c/en/stone\t{"weight":9}',
                '/a/low\t/r/IsA\t/c/en/rock\t/c/en/stone\t{"weight":0}',
                '/a/baduri\t/r/IsA\tnot-a-uri\t/c/en/stone\t{"weight":9}',
                'malformed',
            ])
            output = root / "clean.sqlite"
            report_path = root / "quality.json"
            report = graph_clean.clean_graph(
                source, output, min_weight=1, degree_cap=500,
                quality_report=report_path, sample_size=3)

            self.assertEqual(report["schema_version"], graph_clean.QUALITY_SCHEMA)
            self.assertEqual(report["rules_version"], "clean-v1")
            self.assertEqual(report["output"]["edges"], 1)
            self.assertEqual(report["rules"]["self_loop"], 1)
            self.assertEqual(report["rules"]["non_english"], 1)
            self.assertEqual(report["rules"]["disallowed_relation"], 1)
            self.assertEqual(report["rules"]["low_weight"], 1)
            self.assertEqual(report["rules"]["invalid_uri"], 1)
            self.assertEqual(report["rules"]["duplicate_edges"], 1)
            self.assertEqual(report["counts"]["invalid_rows"], 1)
            self.assertEqual(report["cross_language_edges"], 1)
            self.assertEqual(report["duplicate_rate"], 1 / 7)

            validation = graph_index.validate_index(output, sample=10)
            self.assertEqual(validation["edges"], 1)
            with contextlib.closing(sqlite3.connect(output)) as db:
                evidence = json.loads(db.execute(
                    "SELECT evidence FROM edges").fetchone()[0])
                metadata = dict(db.execute("SELECT key, value FROM metadata"))
            self.assertEqual(evidence["weight"], 3.0)
            self.assertEqual(evidence["source_lines"], [1, 2])
            self.assertEqual(evidence["source_assertions"], ["/a/dup", "/a/good"])
            self.assertEqual(evidence["aliases"]["subject"],
                             ["/c/en/rock", "/c/en/rock/n/wn/object"])
            self.assertEqual(evidence["datasets"], ["/d/x", "/d/y"])
            self.assertEqual(evidence["licenses"], ["cc:by-sa/4.0"])
            self.assertEqual(evidence["rules_version"], "clean-v1")
            self.assertEqual(metadata["rules_version"], "clean-v1")
            self.assertTrue(metadata["parent_graph_id"].startswith("g-"))
            self.assertEqual(metadata["graph_id"], report["output"]["graph_id"])
            self.assertTrue(metadata["extensions_merged"] == "false")
            self.assertEqual(metadata["content_sha256"],
                             report["output"]["content_sha256"])
            manifest = json.loads(metadata["manifest"])
            self.assertEqual(manifest["source_sha256"],
                             hashlib.sha256(source.read_bytes()).hexdigest())
            self.assertEqual(graph_index.derive_graph_id(
                manifest, "clean-v1", report["clean_parameters_sha256"]),
                report["output"]["graph_id"])
            self.assertNotEqual(graph_index.derive_graph_id(manifest, "raw-v1"),
                                report["output"]["graph_id"])
            self.assertEqual(metadata["clean_parameters_sha256"],
                             report["clean_parameters_sha256"])
            self.assertEqual(json.loads(report_path.read_text(encoding="utf-8")),
                             report)
            self.assertNotIn(str(root), report_path.read_text(encoding="utf-8"))

            with self.assertRaises(ValueError):
                graph_clean.clean_graph(source, output, quality_report=None)

            with contextlib.closing(sqlite3.connect(output)) as db:
                db.execute("DELETE FROM metadata WHERE key='clean_parameters_sha256'")
                db.commit()
            with self.assertRaises(ValueError):
                graph_index.validate_index(output, sample=10)


class DegreeCapTests(unittest.TestCase):
    def test_degree_cap_keeps_highest_weight_edges(self):
        with test_directory() as root:
            source = write_assertions(root, [
                f'/a/{weight}\t/r/RelatedTo\t/c/en/hub\t/c/en/n{weight}\t'
                f'{{"weight":{weight},"dataset":"/d/x","license":"cc:by-sa/4.0"}}'
                for weight in (1, 5, 3, 4)
            ])
            report = graph_clean.clean_graph(
                source, root / "clean.sqlite", min_weight=1, degree_cap=2,
                quality_report=root / "quality.json")
            self.assertEqual(report["output"]["edges"], 2)
            self.assertEqual(report["counts"]["degree_cap_nodes"], 1)
            self.assertEqual(report["counts"]["degree_cap_edges"], 2)
            self.assertEqual(report["counts"]["isolated_nodes"], 2)
            with contextlib.closing(sqlite3.connect(root / "clean.sqlite")) as db:
                weights = [json.loads(row[0])["weight"] for row in db.execute(
                    "SELECT evidence FROM edges")]
            self.assertEqual(sorted(weights), [4.0, 5.0])

    def test_cleaning_parameters_change_graph_id(self):
        with test_directory() as root:
            source = write_assertions(root, [
                f'/a/{weight}\t/r/RelatedTo\t/c/en/hub\t/c/en/n{weight}\t'
                f'{{"weight":{weight},"dataset":"/d/x","license":"cc:by-sa/4.0"}}'
                for weight in (1, 2, 3, 4)
            ])
            first = graph_clean.clean_graph(
                source, root / "cap2.sqlite", min_weight=1, degree_cap=2,
                quality_report=root / "cap2.json")
            second = graph_clean.clean_graph(
                source, root / "cap3.sqlite", min_weight=1, degree_cap=3,
                quality_report=root / "cap3.json")
            self.assertNotEqual(first["output"]["graph_id"],
                                second["output"]["graph_id"])
            self.assertNotEqual(first["output"]["content_sha256"],
                                second["output"]["content_sha256"])
            self.assertEqual(first["output"]["parent_graph_id"],
                             second["output"]["parent_graph_id"])
            self.assertEqual(first["cleaning_parameters"]["degree_cap"], 2)
            self.assertEqual(second["cleaning_parameters"]["degree_cap"], 3)
            self.assertNotEqual(first["clean_parameters_sha256"],
                                second["clean_parameters_sha256"])

    def test_same_input_is_byte_deterministic(self):
        with test_directory() as root:
            source = write_assertions(root, [
                '/a/one\t/r/RelatedTo\t/c/en/rock\t/c/en/stone\t'
                '{"weight":2,"dataset":"/d/x","license":"cc:by-sa/4.0"}',
                '/a/two\t/r/IsA\t/c/en/rock\t/c/en/granite\t'
                '{"weight":3,"dataset":"/d/y","license":"cc:by-sa/4.0"}',
            ])
            first = root / "first"
            second = root / "second"
            first.mkdir()
            second.mkdir()
            report_a = graph_clean.clean_graph(
                source, first / "clean.sqlite", min_weight=1, degree_cap=500,
                quality_report=first / "quality.json")
            report_b = graph_clean.clean_graph(
                source, second / "clean.sqlite", min_weight=1, degree_cap=500,
                quality_report=second / "quality.json")
            self.assertEqual(report_a["output"]["content_sha256"],
                             report_b["output"]["content_sha256"])
            self.assertEqual(report_a["output"]["graph_id"],
                             report_b["output"]["graph_id"])
            self.assertEqual((first / "quality.json").read_bytes(),
                             (second / "quality.json").read_bytes())


class ExtensionTests(unittest.TestCase):
    def _extension_file(self, root):
        path = root / "extensions.jsonl"
        rows = [
            {"subject": "robot", "relation": "/r/RelatedTo", "object": "robotics",
             "weight": 5, "confidence": 0.9, "source": "ai-glossary",
             "source_type": "domain_glossary", "license": "CC0-1.0",
             "created_at": "2026-01-01"},
            {"subject": "rock", "relation": "/r/RelatedTo", "object": "robot",
             "weight": 2, "confidence": 0.8, "source": "paper-concepts",
             "source_type": "paper_concepts", "license": "CC-BY-4.0",
             "created_at": "2026-01-02"},
            {"subject": "bad", "relation": "/r/RelatedTo", "object": "edge",
             "weight": 1, "confidence": 0.9, "source": "x",
             "source_type": "domain_glossary", "license": "CC0-1.0"},
            {"subject": "bad", "relation": "/r/RelatedTo", "object": "type",
             "weight": 1, "confidence": 0.9, "source": "x",
             "source_type": "unknown", "license": "CC0-1.0",
             "created_at": "2026-01-01"},
            {"subject": "loop", "relation": "/r/RelatedTo", "object": "loop",
             "weight": 1, "confidence": 0.9, "source": "x",
             "source_type": "human_review", "license": "CC0-1.0",
             "created_at": "2026-01-01"},
            {"subject": "bad-confidence", "relation": "/r/RelatedTo",
             "object": "edge", "weight": 1, "confidence": 2,
             "source": "x", "source_type": "human_review", "license": "CC0-1.0",
             "created_at": "2026-01-01"},
        ]
        path.write_text("\n".join(json.dumps(row) for row in rows) + "\n",
                        encoding="utf-8")
        return path

    def test_extensions_are_separate_by_default_and_opt_in_to_merge(self):
        with test_directory() as root:
            source = write_assertions(root, [
                '/a/base\t/r/IsA\t/c/en/rock\t/c/en/granite\t'
                '{"weight":3,"dataset":"/d/x","license":"cc:by-sa/4.0"}',
            ])
            extensions = self._extension_file(root)
            base = graph_clean.clean_graph(
                source, root / "base.sqlite", min_weight=1, degree_cap=500,
                extensions=[extensions], merge_extensions=False,
                quality_report=root / "base_quality.json")
            self.assertEqual(base["output"]["edges"], 1)
            self.assertEqual(base["counts"]["extension_accepted"], 2)
            self.assertEqual(base["counts"]["extension_missing_created_at"], 1)
            self.assertEqual(base["counts"]["extension_invalid_source_type"], 1)
            self.assertEqual(base["counts"]["extension_self_loop"], 1)
            self.assertEqual(base["counts"]["extension_invalid_confidence"], 1)
            self.assertEqual(base["extensions"]["source_priority"],
                             list(graph_clean.EXTENSION_SOURCE_TYPES))
            with contextlib.closing(sqlite3.connect(root / "base.sqlite")) as db:
                self.assertEqual(db.execute(
                    "SELECT count(*) FROM extensions").fetchone()[0], 2)
                self.assertEqual(dict(db.execute(
                    "SELECT key, value FROM metadata"))["extensions_merged"],
                    "false")
                self.assertEqual(db.execute(
                    "SELECT count(*) FROM edges").fetchone()[0], 1)

            merged = graph_clean.clean_graph(
                source, root / "merged.sqlite", min_weight=1, degree_cap=500,
                extensions=[extensions], merge_extensions=True,
                quality_report=root / "merged_quality.json")
            self.assertEqual(merged["output"]["edges"], 3)
            self.assertNotEqual(merged["output"]["graph_id"],
                                base["output"]["graph_id"])
            self.assertEqual(merged["counts"].get("extension_merged_edges", 0), 0)
            graph_index.validate_index(root / "merged.sqlite", sample=10)
            with contextlib.closing(sqlite3.connect(root / "merged.sqlite")) as db:
                metadata = dict(db.execute("SELECT key, value FROM metadata"))
                self.assertEqual(metadata["extensions_merged"], "true")
                self.assertTrue(metadata["extensions_sha256"])
                evidence = json.loads(db.execute(
                    "SELECT evidence FROM edges WHERE subject='robot'").fetchone()[0])
            self.assertEqual(evidence["source"], "ai-glossary")
            self.assertEqual(evidence["source_type"], "domain_glossary")
            self.assertEqual(evidence["license"], "CC0-1.0")
            self.assertEqual(evidence["created_at"], "2026-01-01")
            self.assertEqual(evidence["confidence"], 0.9)


class DomainAndProviderTests(unittest.TestCase):
    def test_domain_marking_provider_read_and_clean_graph_version(self):
        with test_directory() as root:
            raw, _ = build_raw(root, [
                '/a/a\t/r/RelatedTo\t/c/en/robot\t/c/en/robotics\t'
                '{"weight":3,"dataset":"/d/x","license":"cc:by-sa/4.0"}',
                '/a/b\t/r/RelatedTo\t/c/en/rock\t/c/en/stone\t'
                '{"weight":3,"dataset":"/d/x","license":"cc:by-sa/4.0"}',
            ])
            domain = root / "domain.txt"
            domain.write_text("robot\nrobotics\n", encoding="utf-8")
            clean = root / "clean.sqlite"
            report = graph_clean.clean_graph(
                raw, clean, min_weight=1, degree_cap=500, domain_terms=domain,
                quality_report=root / "quality.json")
            self.assertEqual(report["domain_coverage"], 0.5)
            self.assertEqual(report["rules"]["domain_low_confidence"], 1)
            with contextlib.closing(sqlite3.connect(clean)) as db:
                rows = [json.loads(row[0]) for row in db.execute(
                    "SELECT evidence FROM edges ORDER BY subject")]
            self.assertEqual([(row["subject"], row["domain_relevant"],
                               row["confidence"]) for row in rows],
                             [("robot", True, 1.0), ("rock", False, 0.5)])

            provider = conceptnet_provider.ConceptNet(
                knowledge="local", index=clean, cache_dir=root / "cache",
                timeout=1)
            snapshot = provider.neighbors("robot")
            self.assertEqual(snapshot["backend"], "local")
            self.assertEqual(snapshot["edges"][0]["object"], "robotics")
            self.assertEqual(provider.graph_id, report["output"]["graph_id"])
            self.assertEqual(
                search._document_graph_version(
                    [{"conceptnet": snapshot}], local_graph_id=provider.graph_id),
                report["output"]["graph_id"])
            self.assertNotEqual(provider.graph_id, report["output"]["parent_graph_id"])

            raw_provider = conceptnet_provider.ConceptNet(
                knowledge="local", index=raw, cache_dir=root / "cache_raw",
                timeout=1)
            self.assertEqual(raw_provider.graph_id,
                             report["output"]["parent_graph_id"])


class CliTests(unittest.TestCase):
    def test_clean_graph_command_parses_with_defaults_off(self):
        from src import cli
        args = cli.build_parser().parse_args(
            ["clean-graph", "--input", "raw.sqlite"])
        self.assertEqual(args.output, constants.ROOT / "data" / "conceptnet_clean.sqlite")
        self.assertFalse(args.merge_extensions)
        self.assertEqual(args.extensions, [])
        self.assertEqual(args.degree_cap, 500)


if __name__ == "__main__":
    unittest.main()
