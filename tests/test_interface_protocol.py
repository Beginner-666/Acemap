"""Interface contract tests for the frozen v2/v3 protocols and M0 migrations."""

from __future__ import annotations

import argparse
import contextlib
import copy
import hashlib
import io
import json
import math
import shutil
import sqlite3
import unittest
import uuid
from pathlib import Path

from src import (cache_migration, conceptnet_index, config, constants,
                 graph_index, jsonio, paper_features, rankers, records,
                 search, verification, versions)


@contextlib.contextmanager
def test_directory():
    root = constants.ROOT / "tests" / ("work_" + uuid.uuid4().hex)
    root.mkdir()
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


MANIFEST = {
    "schema_version": 1,
    "source_name": "fixture assertions",
    "source_url": "https://example.invalid/assertions",
    "source_sha256": "a" * 64,
    "source_file": "fixture.tsv",
    "rows_scanned": 3,
    "edges_indexed": 1,
    "language": "en",
    "relations": ["/r/RelatedTo"],
    "min_weight": 1,
}

EDGE = {
    "assertion": "/a/1",
    "relation": "/r/RelatedTo",
    "subject": "rock",
    "object": "stone",
    "subject_uri": "/c/en/rock",
    "object_uri": "/c/en/stone",
    "weight": 2.0,
    "source_line": 1,
    "metadata": {"weight": 2},
}


def v3_document():
    resolved = config.resolve_for_ranker(config.default_ranking_config(), "rrf")
    return {
        "schema_version": versions.OUTPUT_SCHEMA_V3,
        "domain": "query-time English ConceptNet",
        "endpoint": constants.ENDPOINT,
        "settings": {
            "knowledge": "auto",
            "size_per_term": 10,
            "max_expansions": 3,
            "lexical_forms": "ai -> artificial intelligence; exact phrases only",
            "fusion": resolved["fusion"],
            "ranker": "rrf",
            "ranker_version": "rrf-v1",
            "ranker_config": resolved,
            "config_hash": versions.config_hash(resolved),
            "graph_version": "g-fixture",
            "snapshot_key_schema": versions.SNAPSHOT_KEY_SCHEMA,
            "record_feature_version": versions.RECORD_FEATURE_VERSION,
        },
        "summary": {"queries": 1, "successful_queries": 1,
                    "successful_with_enhancement": 1},
        "searches": [{
            "query": "rock",
            "status": "ok",
            "enhancement_applied": True,
            "expansions": [{"term": "stone", "evidence": EDGE,
                            "domain_support_seeds": []}],
            "warning": None,
            "requests": [
                {"term": "rock", "status": "ok", "count": 1, "cache_file": "a.json"},
                {"term": "stone", "status": "ok", "count": 1, "cache_file": "b.json"},
            ],
            "metrics": {"baseline_unique": 1, "enhanced_unique": 1,
                        "additional_unique": 0, "failed_requests": 0},
            "results": [{
                "key": "id:one",
                "paper": {
                    "id": "one",
                    "title": "Rock",
                    "authorships": [{
                        "author": {"id": "a1", "display_name": "Alice"},
                        "institutions": [{"id": "i1", "display_name": "Toronto"}],
                    }],
                },
                "score": 1.0,
                "score_components": {"acemap": 1.0, "graph": 0.0, "text": 0.0,
                                     "freshness": 0.0, "personalization": 0.0},
                "evidence": {"matched_terms": ["rock"], "graph_paths": [],
                             "matched_authors": [{"id": "a1", "name": "Alice"}],
                             "matched_institutions": [{"id": "i1", "name": "Toronto"}]},
                "matches": [{"term": "rock", "rank": 1, "is_original": True}],
            }],
            "conceptnet": {
                "query": "rock",
                "matched_concept": "rock",
                "forms_checked": ["rock"],
                "backend": "local",
                "source": dict(MANIFEST),
                "diagnostics": [],
                "edges": [dict(EDGE)],
                "scope": verification.SCOPE,
            },
        }],
    }


class ProtocolValidationTests(unittest.TestCase):
    def test_v2_golden_validates(self):
        document = jsonio.read_json(constants.ROOT / "results/dynamic_queries.json")
        self.assertIs(verification.validate_v2_document(document), document)

    def test_v2_rejects_v3_field(self):
        document = jsonio.read_json(constants.ROOT / "results/dynamic_queries.json")
        tampered = copy.deepcopy(document)
        tampered["searches"][0]["results"][0]["score"] = 1.0
        with self.assertRaises(ValueError):
            verification.validate_v2_document(tampered)

    def test_v2_rejects_tampered_fusion_score(self):
        document = jsonio.read_json(constants.ROOT / "results/dynamic_queries.json")
        tampered = copy.deepcopy(document)
        tampered["searches"][0]["results"][0]["fusion_score"] += 0.1
        with self.assertRaises(ValueError):
            verification.validate_v2_document(tampered)

    def test_v2_rejects_schema_version(self):
        document = jsonio.read_json(constants.ROOT / "results/dynamic_queries.json")
        document["schema_version"] = versions.OUTPUT_SCHEMA_V3
        with self.assertRaises(ValueError):
            verification.validate_v2_document(document)

    def test_v2_rejects_missing_required_field(self):
        document = jsonio.read_json(constants.ROOT / "results/dynamic_queries.json")
        del document["settings"]["fusion"]
        with self.assertRaises(ValueError):
            verification.validate_v2_document(document)

    def test_v2_rejects_query_set_mismatch(self):
        with test_directory() as root:
            queries = root / "queries.txt"
            queries.write_text("wood\n", encoding="utf-8")
            args = argparse.Namespace(
                results=constants.ROOT / "results/dynamic_queries.json",
                queries=queries, cache=constants.DEFAULT_ACEAMP_CACHE,
                conceptnet_cache=constants.DEFAULT_CONCEPTNET_CACHE,
                index=constants.DEFAULT_INDEX)
            document = jsonio.read_json(args.results)
            with self.assertRaises(ValueError):
                verification.verify_v2(args, document)

    def test_failure_path_emits_valid_v2(self):
        with test_directory() as root:
            (root / "cn").mkdir()
            (root / "am").mkdir()
            output = root / "out.json"
            args = argparse.Namespace(
                query="no-such-query", queries=None, knowledge="auto",
                index=root / "missing.sqlite", conceptnet_cache=root / "cn",
                cache=root / "am", output=output, max_expansions=3, size=10,
                timeout=1, retries=0, cache_only=True, refresh=False)
            with contextlib.redirect_stdout(io.StringIO()):
                code = search.run_search(args)
            self.assertEqual(code, 1)
            document = jsonio.read_json(output)
            self.assertIs(verification.validate_v2_document(document), document)
            self.assertEqual(document["searches"][0]["conceptnet"]["backend"], "none")
            self.assertEqual(document["searches"][0]["conceptnet"]["source"], None)
            self.assertEqual(document["searches"][0]["conceptnet"]["scope"],
                             verification.SCOPE)

    def test_v3_synthetic_validates(self):
        document = v3_document()
        self.assertIs(verification.validate_v3_document(document), document)

    def test_v3_rejects_bad_config_hash(self):
        document = v3_document()
        document["settings"]["config_hash"] = "sha256:bad"
        with self.assertRaises(ValueError):
            verification.validate_v3_document(document)

    def test_v3_rejects_bad_component_sum(self):
        document = v3_document()
        document["searches"][0]["results"][0]["score"] = 0.5
        with self.assertRaises(ValueError):
            verification.validate_v3_document(document)

    def test_v3_rejects_untraceable_path(self):
        document = v3_document()
        document["searches"][0]["results"][0]["evidence"]["graph_paths"] = [{
            "nodes": ["rock", "stone"],
            "relations": ["/r/RelatedTo"],
            "weight": 2.0,
            "score": 1.0,
            "assertions": ["/a/missing"],
        }]
        with self.assertRaises(ValueError):
            verification.validate_v3_document(document)

    def test_v3_rejects_matched_term_outside_expansions(self):
        document = v3_document()
        document["searches"][0]["results"][0]["evidence"]["matched_terms"] = ["robot"]
        with self.assertRaises(ValueError):
            verification.validate_v3_document(document)

    def test_v3_rejects_untraceable_author(self):
        document = v3_document()
        document["searches"][0]["results"][0]["evidence"]["matched_authors"] = [
            {"id": "missing", "name": "Nobody"}]
        with self.assertRaises(ValueError):
            verification.validate_v3_document(document)

    def test_v3_rejects_missing_assertions(self):
        document = v3_document()
        path = {"nodes": ["rock", "stone"], "relations": ["/r/RelatedTo"],
                "weight": 2.0, "score": 1.0}
        document["searches"][0]["results"][0]["evidence"]["graph_paths"] = [path]
        with self.assertRaises(ValueError):
            verification.validate_v3_document(document)

    def test_v3_rejects_bogus_ranker_version(self):
        document = v3_document()
        document["settings"]["ranker_version"] = "bogus-v9"
        with self.assertRaises(ValueError):
            verification.validate_v3_document(document)

    def test_v3_golden_replays_byte_identical(self):
        results = constants.ROOT / "results/dynamic_queries_v3.json"
        args = argparse.Namespace(
            results=results, queries=constants.ROOT / "config/dynamic_queries.txt",
            cache=constants.DEFAULT_ACEAMP_CACHE,
            conceptnet_cache=constants.DEFAULT_CONCEPTNET_CACHE,
            index=constants.DEFAULT_INDEX)
        document = jsonio.read_json(results)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(verification.verify_v3(args, document), 0)
        self.assertTrue(results.with_name(
            results.stem + "_replay.json").exists())


class RankerTests(unittest.TestCase):
    def test_rrf_merge_matches_explicit_formula(self):
        term_results = [
            {"term": "rock", "is_original": True, "status": "ok",
             "records": [{"id": "a"}, {"id": "b"}, {"id": "a"}],
             "cache_file": "f.json", "error": None},
            {"term": "robot", "is_original": False, "status": "ok",
             "records": [{"id": "b"}, {"id": "c"}],
             "cache_file": "g.json", "error": None},
        ]
        ranked, metrics = rankers.rrf_merge("rock", term_results)
        self.assertEqual([item["key"] for item in ranked], ["id:b", "id:a", "id:c"])
        self.assertTrue(math.isclose(ranked[0]["fusion_score"],
                                     1 / 62 + 0.5 / 61, rel_tol=0, abs_tol=1e-15))
        self.assertEqual(ranked[0]["matches"], [
            {"term": "rock", "rank": 2, "is_original": True},
            {"term": "robot", "rank": 1, "is_original": False},
        ])
        self.assertEqual(metrics, {"baseline_unique": 2, "enhanced_unique": 3,
                                   "additional_unique": 1, "failed_requests": 0})

    def test_search_one_uses_the_same_merge(self):
        class Client:
            def search(self, term, size):
                if term == "rock":
                    return [{"id": "a"}, {"id": "b"}, {"id": "a"}], "f.json"
                return [{"id": "b"}, {"id": "c"}], "g.json"

        expansion = [{"term": "robot", "evidence": EDGE, "domain_support_seeds": []}]
        result = search.search_one(Client(), "rock", 10, expansion)
        self.assertEqual([item["key"] for item in result["results"]],
                         ["id:b", "id:a", "id:c"])
        self.assertEqual(result["metrics"], {"baseline_unique": 2, "enhanced_unique": 3,
                                             "additional_unique": 1, "failed_requests": 0})

    def test_rrf_ranker_emits_v3_shape(self):
        term_results = [
            {"term": "rock", "is_original": True, "status": "ok",
             "records": [{"id": "a"}, {"id": "b"}], "cache_file": "f.json", "error": None},
            {"term": "robot", "is_original": False, "status": "ok",
             "records": [{"id": "b"}], "cache_file": "g.json", "error": None},
        ]
        output = rankers.RRFRanker().rank("rock", term_results)
        first = output["results"][0]
        self.assertEqual(list(first), ["key", "paper", "score", "score_components",
                                       "evidence", "matches"])
        self.assertEqual(list(first["score_components"]), list(verification.COMPONENT_KEYS))
        self.assertEqual(first["score"], first["score_components"]["acemap"])
        self.assertEqual(first["score_components"]["acemap"], 1.0)
        self.assertTrue(all(first["score_components"][key] == 0.0
                            for key in verification.COMPONENT_KEYS[1:]))

    def test_baseline_ranker_preserves_acemap_order(self):
        term_results = [
            {"term": "rock", "is_original": True, "status": "ok",
             "records": [{"id": "a"}, {"id": "b"}], "cache_file": "f.json", "error": None},
        ]
        output = rankers.BaselineRanker().rank("rock", term_results)
        self.assertEqual([item["key"] for item in output["results"]], ["id:a", "id:b"])
        self.assertEqual(output["results"][0]["score"], 1.0)
        self.assertTrue(math.isclose(output["results"][1]["score"], 61 / 62,
                                     rel_tol=0, abs_tol=1e-12))

    def test_unknown_ranker_fails(self):
        with self.assertRaises(ValueError):
            rankers.get_ranker("unknown")


class ConfigTests(unittest.TestCase):
    def test_default_config_is_valid(self):
        loaded = config.load_ranking_config()
        self.assertEqual(loaded["fusion"]["k"], 60)
        resolved = config.resolve_for_ranker(loaded, "rrf")
        self.assertEqual(resolved["weights"]["acemap"], 1.0)
        self.assertEqual(resolved["weights"]["graph"], 0.0)

    def test_weights_must_sum_to_one(self):
        broken = config.default_ranking_config()
        broken["weights"]["acemap"] = 0.9
        with self.assertRaises(ValueError):
            config.validate_ranker_config(broken)

    def test_unknown_key_is_rejected(self):
        broken = config.default_ranking_config()
        broken["unexpected"] = 1
        with self.assertRaises(ValueError):
            config.validate_ranker_config(broken)


class PaperFeatureTests(unittest.TestCase):
    def test_realistic_record(self):
        record = {
            "id": "one",
            "title": "Animatronics",
            "abstract": "A survey.",
            "publication_date": "2023-09-08",
            "doi": "https://doi.org/10.1234/ABC",
            "keywords": ["Robot", "robot", ""],
            "concepts": [{"id": "c1", "display_name": "Computer science",
                          "score": 0.3, "level": 0, "wikidata": "Q1"}],
            "topics": [{"display_name": "Robotics",
                        "field": {"display_name": "Engineering"},
                        "subfield": {"display_name": "Mechanical"},
                        "domain": {"display_name": "Physical Sciences"}}],
            "authorships": [{
                "author": {"id": "a1", "display_name": "Todd", "orcid": None},
                "author_position": "first",
                "institutions": [{"id": "i1", "display_name": "Toronto",
                                  "ror": "r", "country_code": "CA",
                                  "type": "education", "lineage": ["i1"]}],
            }],
            "primary_location": {"source": {"display_name": "Routledge"}},
            "cited_by_count": 4.0,
        }
        features = paper_features.paper_features(record)
        self.assertEqual(features["year"], 2023)
        self.assertEqual(features["doi"], "10.1234/abc")
        self.assertEqual(features["keywords"], ["Robot"])
        self.assertEqual(features["concepts"][0]["display_name"], "Computer science")
        self.assertEqual(features["topics"][0]["field"], "Engineering")
        self.assertEqual(features["authors"][0]["id"], "a1")
        self.assertEqual(features["authors"][0]["display_name"], "Todd")
        self.assertEqual(features["authors"][0]["author_position"], "first")
        self.assertEqual(features["institutions"][0]["country_code"], "CA")
        self.assertEqual(features["venue"], "Routledge")
        self.assertEqual(features["cited_by_count"], 4)
        self.assertEqual(records.identity({"doi": "https://doi.org/10.1234/ABC"}),
                         "doi:10.1234/abc")

    def test_missing_fields_degrade(self):
        features = paper_features.paper_features({})
        self.assertEqual(features["title"], "")
        self.assertIsNone(features["year"])
        self.assertIsNone(features["doi"])
        self.assertEqual(features["authors"], [])
        self.assertEqual(features["institutions"], [])


class GraphIndexTests(unittest.TestCase):
    def test_build_read_and_validate(self):
        with test_directory() as root:
            source = root / "source.tsv"
            source.write_text(
                '/a/good\t/r/IsA\t/c/en/rock/n/wn/object\t/c/en/granite/n/wn/substance\t{"weight":3}\n',
                encoding="utf-8")
            index = root / "index.sqlite"
            with contextlib.redirect_stdout(io.StringIO()):
                conceptnet_index.build_index(source, index)
            with contextlib.closing(sqlite3.connect(index)) as db:
                row = db.execute(
                    "SELECT value FROM metadata WHERE key='graph_id'").fetchone()
                self.assertIsNotNone(row)
                self.assertTrue(row[0].startswith("g-"))
                self.assertEqual(graph_index.read_graph_id(db), row[0])
                self.assertEqual(graph_index.edges_for_term(db, "rock")[0]["object"],
                                 "granite")
            report = graph_index.validate_index(index, sample=10)
            self.assertEqual(report["edges"], 1)
            self.assertEqual(report["sampled"], 1)
            self.assertTrue(graph_index.ensure_graph_id(index)["existing"])

    def test_stored_graph_id_mismatch_is_rejected(self):
        with test_directory() as root:
            source = root / "source.tsv"
            source.write_text(
                '/a/good\t/r/IsA\t/c/en/rock\t/c/en/granite\t{"weight":3}\n',
                encoding="utf-8")
            index = root / "index.sqlite"
            with contextlib.redirect_stdout(io.StringIO()):
                conceptnet_index.build_index(source, index)
            with contextlib.closing(sqlite3.connect(index)) as db:
                db.execute("UPDATE metadata SET value=? WHERE key='graph_id'",
                           ("g-deadbeefdeadbeef",))
                db.commit()
            with self.assertRaises(ValueError):
                graph_index.validate_index(index, sample=10)


class CacheMigrationTests(unittest.TestCase):
    def _write_legacy(self, root, mode="auto"):
        snapshot = {
            "query": "rock", "matched_concept": "rock",
            "forms_checked": ["rock"], "backend": "local",
            "source": dict(MANIFEST), "diagnostics": [],
            "edges": [dict(EDGE)], "scope": verification.SCOPE,
        }
        old = root / (cache_migration._old_key(mode, "rock") + ".json")
        jsonio.write_json(old, snapshot)
        return old

    def test_plan_apply_idempotent_and_rollback(self):
        with test_directory() as root:
            old = self._write_legacy(root)
            before = old.read_bytes()
            actions = cache_migration.plan(root)
            self.assertEqual(len(actions), 1)
            self.assertEqual(actions[0]["status"], "planned")
            self.assertEqual(actions[0]["mode"], "auto")
            self.assertTrue(actions[0]["new"].endswith(".json"))
            self.assertEqual(actions[0]["sha256"],
                             hashlib.sha256(before).hexdigest())
            self.assertEqual(actions[0]["graph_id"],
                             graph_index.derive_graph_id(MANIFEST))
            applied = cache_migration.apply(root, actions)
            self.assertEqual(applied[0]["status"], "renamed")
            new = root / applied[0]["new"]
            self.assertFalse(old.exists())
            self.assertEqual(new.read_bytes(), before)
            # A second run is a no-op: new names are not legacy keys.
            second = cache_migration.plan(root)
            self.assertEqual(second[0]["status"], "skipped")
            manifest_path = root / "manifest.json"
            jsonio.write_json(manifest_path, cache_migration.manifest(root, applied))
            restored = cache_migration.rollback(manifest_path)
            self.assertEqual(restored, [old.name])
            self.assertTrue(old.exists())
            self.assertFalse(new.exists())

    def test_conflict_aborts_before_rename(self):
        with test_directory() as root:
            old = self._write_legacy(root)
            action = cache_migration.plan(root)[0]
            target = root / action["new"]
            target.write_bytes(b"different")
            with self.assertRaises(ValueError):
                cache_migration.apply(root)
            self.assertTrue(old.exists())
            self.assertEqual(target.read_bytes(), b"different")


class EvaluationTests(unittest.TestCase):
    def test_query_qrels_and_config_loaders(self):
        with test_directory() as root:
            queries_path = root / "queries.json"
            qrels_path = root / "qrels.json"
            config_path = root / "evaluation.json"
            jsonio.write_json(queries_path, {"queries": [{
                "id": "q1", "text": "rock", "topic": "rock",
                "time_filter": None, "task": "research", "notes": "",
            }]})
            jsonio.write_json(qrels_path, {"q1": {"doi:10.1/x": 2}})
            jsonio.write_json(config_path, {
                "k_values": [5, 10], "metrics": ["ndcg"], "seed": 0,
                "splits": {"type": "time", "test_after": "2025-01-01"},
            })
            queries = evaluation_load(queries_path)
            self.assertEqual(queries[0]["id"], "q1")
            qrels = evaluation_qrels(qrels_path, queries)
            self.assertEqual(qrels["q1"]["doi:10.1/x"], 2)
            self.assertEqual(evaluation_config(config_path)["k_values"], [5, 10])

    def test_invalid_grade_and_unknown_query(self):
        with test_directory() as root:
            qrels_path = root / "qrels.json"
            jsonio.write_json(qrels_path, {"q1": {"doi:10.1/x": 4}})
            with self.assertRaises(ValueError):
                evaluation_qrels(qrels_path)
            jsonio.write_json(qrels_path, {"unknown": {"doi:10.1/x": 1}})
            with self.assertRaises(ValueError):
                evaluation_qrels(qrels_path, [{"id": "q1"}])


def evaluation_load(path):
    from src import evaluation
    return evaluation.load_queries(path)


def evaluation_qrels(path, queries=None):
    from src import evaluation
    return evaluation.load_qrels(path, queries)


def evaluation_config(path):
    from src import evaluation
    return evaluation.load_evaluation_config(path)


if __name__ == "__main__":
    unittest.main()
