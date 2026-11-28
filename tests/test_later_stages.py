import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from src import cli
from src.graph_algorithms import ALGORITHMS, analyze_graph, rank_terms
from src.profile import build_profile, personalize_results, validate_document
from src.rag import build_context, build_document, extractive_answer, generate, validate_document as validate_rag


EDGES = [
    {"subject": "robotics", "object": "robot", "relation": "/r/RelatedTo",
     "assertion": "/a/robot", "weight": 2},
    {"subject": "robot", "object": "learning", "relation": "/r/IsA",
     "assertion": "/a/learning", "weight": 1},
    {"subject": "robotics", "object": "ai", "relation": "/r/IsA",
     "assertion": "/a/ai", "weight": 3},
]


class GraphAlgorithmTests(unittest.TestCase):
    def test_algorithms_are_deterministic_and_bounded(self):
        for algorithm in ALGORITHMS:
            kwargs = {"relation_sequence": ["/r/RelatedTo", "/r/IsA"]} \
                if algorithm == "meta_path" else {}
            first = rank_terms(EDGES, ["robotics"], algorithm, 10, **kwargs)
            second = rank_terms(EDGES, ["robotics"], algorithm, 10, **kwargs)
            self.assertEqual(first, second)
            self.assertTrue(all(0 < item["score"] <= 1 for item in first))
            self.assertTrue(all(item["algorithm"] == algorithm for item in first))

    def test_analysis_document_has_replay_parameters(self):
        document = analyze_graph(EDGES, ["robotics"], "pagerank", 2,
                                 damping=.8, iterations=12, tolerance=1e-9)
        self.assertEqual(document["schema_version"], "graph-analysis-v1")
        self.assertEqual(document["summary"]["candidates"], 2)
        self.assertEqual(document["parameters"]["iterations"], 12)


class RagTests(unittest.TestCase):
    def test_extractive_answer_is_grounded(self):
        context = build_context(
            "robotics",
            graph_facts=[{"subject": "robotics", "relation": "/r/RelatedTo",
                          "object": "robot", "source": "/a/robot"}],
            papers=[{"id": "p1", "title": "Robotics", "snippet": "A study.",
                     "citation": "doi:10/example"}],
        )
        output = build_document(context, generate(context))
        validate_rag(output)
        self.assertTrue(output["quality"]["grounded"])
        self.assertEqual(output["quality"]["citation_count"], 2)

    def test_generator_failure_falls_back(self):
        context = build_context("robotics")
        generated = generate(context, lambda _: (_ for _ in ()).throw(RuntimeError("offline")))
        self.assertEqual(generated["mode"], "extractive_fallback")
        self.assertTrue(generated["answer"]["insufficient_evidence"])


class ProfileTests(unittest.TestCase):
    def test_consent_and_time_decay(self):
        history = [{"record": {"id": "h", "title": "Robotics",
                                "keywords": ["robotics"]}, "year": 2024}]
        disabled = build_profile(history, consent=False)
        self.assertFalse(disabled["enabled"])
        profile = build_profile(history, consent=True, reference_year=2025)
        self.assertTrue(profile["enabled"])
        result = {"key": "p", "paper": {"id": "p", "title": "Robotics",
                                            "keywords": ["robotics"]}, "score": 1.0}
        personalized = personalize_results([result], profile, alpha=.2)
        self.assertGreater(personalized[0]["profile_score"], 0)
        self.assertAlmostEqual(personalized[0]["score"],
                               .8 + .2 * personalized[0]["profile_score"])

    def test_personalized_document_validation(self):
        profile = build_profile([], consent=False)
        document = {"schema_version": "personalized-v1", "profile": profile,
                    "settings": {"alpha": .2}, "summary": {},
                    "searches": [{"query": "x", "results": personalize_results(
                        [{"key": "p", "paper": {}, "score": .5}], profile)}]}
        validate_document(document)


class LaterCliTests(unittest.TestCase):
    def test_commands_are_available(self):
        parser = cli.build_parser()
        self.assertEqual(parser.parse_args(["rag", "--query", "robotics"]).command, "rag")
        self.assertEqual(parser.parse_args(["profile", "--history", "h.json"]).command,
                         "profile")
        self.assertEqual(parser.parse_args(["personalize", "--results", "r.json"]).command,
                         "personalize")
        self.assertEqual(parser.parse_args(["graph-analyze", "--edges", "e.json",
                                            "--seeds", "robotics", "--algorithm", "community"]).command,
                         "graph-analyze")


if __name__ == "__main__":
    unittest.main()
