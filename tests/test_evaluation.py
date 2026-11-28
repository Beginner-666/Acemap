"""Evaluation metrics, qrels-driven reports, and global hub filtering."""

from __future__ import annotations

import contextlib
import io
import json
import math
import shutil
import sqlite3
import unittest
import uuid
from contextlib import closing
from pathlib import Path

from src import constants, graph_index, jsonio
from src.conceptnet_index import build_index
from src.evaluation import evaluate_documents, query_metrics
from src.query_understanding import select_expansions


@contextlib.contextmanager
def test_directory():
    root = constants.ROOT / "tests" / ("work_" + uuid.uuid4().hex)
    root.mkdir()
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


def result_document(ranker, rows):
    return {
        "schema_version": 3,
        "settings": {"ranker": ranker},
        "searches": rows,
    }


class MetricTests(unittest.TestCase):
    def test_precision_recall_ndcg_mrr(self):
        labels = {"a": 3, "c": 1, "x": 2}
        metrics = query_metrics(["a", "b", "c", "d"], labels, [2, 4], 1)
        self.assertEqual(metrics["precision@2"], 0.5)
        self.assertAlmostEqual(metrics["recall@2"], 1.0 / 3.0)
        self.assertEqual(metrics["mrr@2"], 1.0)
        expected_ndcg = 7.0 / (7.0 + 3.0 / math.log2(3))
        self.assertAlmostEqual(metrics["ndcg@2"], expected_ndcg)
        self.assertEqual(metrics["precision@4"], 0.5)
        self.assertAlmostEqual(metrics["recall@4"], 2.0 / 3.0)
        self.assertEqual(metrics["unjudged@4"], 2)
        self.assertGreater(metrics["ndcg@4"], metrics["ndcg@2"])

    def test_threshold_filters_low_grades(self):
        labels = {"a": 1, "b": 2}
        metrics = query_metrics(["a", "b"], labels, [2], 2)
        self.assertEqual(metrics["precision@2"], 0.5)
        self.assertEqual(metrics["recall@2"], 1.0)
        self.assertEqual(metrics["mrr@2"], 0.5)

    def test_evaluate_documents_comparison(self):
        queries = [
            {"id": "q1", "text": "rock", "topic": "rock", "time_filter": None,
             "task": "research", "notes": ""},
            {"id": "q2", "text": "robot", "topic": "robot", "time_filter": None,
             "task": "research", "notes": ""},
        ]
        qrels = {"q1": {"a": 3, "b": 1}, "q2": {"c": 2, "d": 1}}
        baseline = result_document("baseline", [
            {"query": "rock", "results": [{"key": "a"}, {"key": "x"}]},
            {"query": "robot", "results": [{"key": "x"}, {"key": "c"}]},
        ])
        rerank = result_document("graph_rerank", [
            {"query": "rock", "results": [{"key": "b"}, {"key": "a"}]},
            {"query": "robot", "results": [{"key": "c"}, {"key": "d"}]},
        ])
        config = {"k_values": [2], "metrics": ["ndcg"], "seed": 0,
                  "splits": {"type": "time"}, "relevance_threshold": 1}
        report = evaluate_documents(
            queries, qrels, {"baseline": baseline, "graph_rerank": rerank}, config)
        self.assertEqual(report["queries"]["count"], 2)
        self.assertGreater(
            report["rankers"]["graph_rerank"]["metrics"]["ndcg@2"],
            report["rankers"]["baseline"]["metrics"]["ndcg@2"])
        self.assertGreater(
            report["comparison"]["graph_rerank"]["ndcg@2"]["delta"], 0.0)

    def test_missing_qrels_fails_explicitly(self):
        queries = [{"id": "q1", "text": "rock", "topic": "rock",
                    "time_filter": None, "task": "research", "notes": ""}]
        document = result_document("baseline", [
            {"query": "rock", "results": [{"key": "a"}]}])
        config = {"k_values": [1], "metrics": ["ndcg"], "seed": 0,
                  "splits": {"type": "time"}}
        with self.assertRaises(ValueError):
            evaluate_documents(queries, {}, {"baseline": document}, config)


class HubFilterTests(unittest.TestCase):
    def test_global_degree_filters_a_local_low_degree_edge(self):
        edge = {"assertion": "/a/1", "relation": "/r/RelatedTo",
                "subject": "robot", "object": "science",
                "subject_uri": "/c/en/robot", "object_uri": "/c/en/science",
                "weight": 9.0}
        selected = select_expansions(
            [edge], "robot", 5, min_edge_weight=1, degree_cap=500,
            degrees={"science": 1000})
        self.assertEqual(selected, [])

    def test_node_degrees_uses_both_endpoints(self):
        with test_directory() as root:
            source = root / "source.tsv"
            source.write_text(
                "/a/1\t/r/IsA\t/c/en/rock\t/c/en/granite\t{\"weight\":3}\n"
                "/a/2\t/r/RelatedTo\t/c/en/stone\t/c/en/rock\t{\"weight\":2}\n",
                encoding="utf-8")
            index = root / "index.sqlite"
            with contextlib.redirect_stdout(io.StringIO()):
                build_index(source, index)
            with closing(sqlite3.connect(index)) as db:
                degrees = graph_index.node_degrees(db, ["rock", "granite", "missing"])
            self.assertEqual(degrees["rock"], 2)
            self.assertEqual(degrees["granite"], 1)
            self.assertEqual(degrees["missing"], 0)


class EvaluationCliTests(unittest.TestCase):
    def test_evaluate_command_parses(self):
        from src.cli import build_parser

        parser = build_parser()
        args = parser.parse_args([
            "evaluate",
            "--queries", "config/evaluation_queries.json",
            "--qrels", "config/qrels.json",
            "--results", "baseline=results/eval_baseline.json",
            "--results", "graph_rerank=results/eval_graph_rerank.json",
        ])
        self.assertEqual(args.command, "evaluate")
        self.assertEqual(len(args.results), 2)

    def test_report_is_deterministic_json(self):
        queries = [{"id": "q1", "text": "rock", "topic": "rock",
                    "time_filter": None, "task": "research", "notes": ""}]
        qrels = {"q1": {"a": 3}}
        document = result_document("baseline", [
            {"query": "rock", "results": [{"key": "a"}]}])
        config = {"k_values": [1], "metrics": ["ndcg"], "seed": 0,
                  "splits": {"type": "time"}}
        report = evaluate_documents(
            queries, qrels, {"baseline": document}, config)
        first = json.dumps(report, ensure_ascii=False, sort_keys=True)
        second = json.dumps(report, ensure_ascii=False, sort_keys=True)
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
