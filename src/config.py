"""Strict loading and validation of ranking configuration.

The running search path does not consume this yet; M1 resolves a ranker config
and embeds it in v3 output.  See docs/interface_protocol.md §4.2.
"""

from __future__ import annotations

import copy
import json
import math
from pathlib import Path

from . import constants
from .versions import config_hash

DEFAULT_RANKING_CONFIG = {
    "fusion": {"k": 60, "original_weight": 1.0, "expansion_weight": 0.5},
    "weights": {"acemap": 0.45, "graph": 0.25, "text": 0.20,
                "freshness": 0.10, "personalization": 0.0},
    "graph": {
        "max_path_length": 1,
        "relation_whitelist": ["/r/IsA", "/r/PartOf", "/r/HasA", "/r/RelatedTo",
                               "/r/Synonym", "/r/DerivedFrom", "/r/FormOf"],
        "degree_cap": 500,
        "min_edge_weight": 1,
    },
    "freshness": {"half_life_years": 5.0, "enabled_by_default": False},
    "personalization": {"alpha": 0.0},
}

_BASELINE_WEIGHTS = {"acemap": 1.0, "graph": 0.0, "text": 0.0,
                     "freshness": 0.0, "personalization": 0.0}


def default_ranking_config():
    return copy.deepcopy(DEFAULT_RANKING_CONFIG)


def _require_keys(value, keys, context):
    if not isinstance(value, dict):
        raise ValueError(f"{context} must be an object")
    if set(value) != set(keys):
        raise ValueError(f"{context} keys must be exactly {sorted(keys)}")


def _number(value, context, minimum=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{context} must be a number")
    if not math.isfinite(float(value)):
        raise ValueError(f"{context} must be finite")
    if minimum is not None and value < minimum:
        raise ValueError(f"{context} must be >= {minimum}")
    return float(value)


def validate_ranker_config(config, ranker="graph_rerank"):
    """Validate the frozen ranker-config shape and numeric invariants."""
    _require_keys(config, ("fusion", "weights", "graph", "freshness", "personalization"),
                  "ranker_config")
    _require_keys(config["fusion"], ("k", "original_weight", "expansion_weight"),
                  "ranker_config.fusion")
    _require_keys(config["weights"], ("acemap", "graph", "text", "freshness",
                                      "personalization"), "ranker_config.weights")
    _require_keys(config["graph"], ("max_path_length", "relation_whitelist",
                                    "degree_cap", "min_edge_weight"),
                  "ranker_config.graph")
    _require_keys(config["freshness"], ("half_life_years", "enabled_by_default"),
                  "ranker_config.freshness")
    _require_keys(config["personalization"], ("alpha",),
                  "ranker_config.personalization")

    k = config["fusion"]["k"]
    if isinstance(k, bool) or not isinstance(k, int) or k < 1:
        raise ValueError("fusion.k must be an integer >= 1")
    _number(config["fusion"]["original_weight"], "fusion.original_weight", 0)
    _number(config["fusion"]["expansion_weight"], "fusion.expansion_weight", 0)

    weights = {key: _number(value, f"weights.{key}", 0)
               for key, value in config["weights"].items()}
    if abs(sum(weights.values()) - 1.0) > 1e-9:
        raise ValueError("weights must sum to 1.0")
    alpha = _number(config["personalization"]["alpha"], "personalization.alpha", 0)
    if alpha > 1:
        raise ValueError("personalization.alpha must be <= 1")
    if abs(weights["personalization"] - alpha) > 1e-9:
        raise ValueError("weights.personalization must equal personalization.alpha")
    if abs(sum(weights[key] for key in weights if key != "personalization")
            - (1 - alpha)) > 1e-9:
        raise ValueError("non-personalization weights must sum to 1 - alpha")
    if ranker in ("baseline", "rrf"):
        for key, expected in _BASELINE_WEIGHTS.items():
            if abs(weights[key] - expected) > 1e-9:
                raise ValueError(f"{ranker} requires weights.{key} == {expected}")

    max_path = config["graph"]["max_path_length"]
    if isinstance(max_path, bool) or not isinstance(max_path, int) or max_path < 1:
        raise ValueError("graph.max_path_length must be an integer >= 1")
    if not isinstance(config["graph"]["relation_whitelist"], list) or not all(
            isinstance(item, str) and item for item in config["graph"]["relation_whitelist"]):
        raise ValueError("graph.relation_whitelist must be a list of strings")
    degree_cap = config["graph"]["degree_cap"]
    if degree_cap is not None and (isinstance(degree_cap, bool)
                                   or not isinstance(degree_cap, int) or degree_cap < 1):
        raise ValueError("graph.degree_cap must be null or an integer >= 1")
    _number(config["graph"]["min_edge_weight"], "graph.min_edge_weight", 0)
    _number(config["freshness"]["half_life_years"], "freshness.half_life_years", 0)
    if config["freshness"]["half_life_years"] <= 0:
        raise ValueError("freshness.half_life_years must be > 0")
    if not isinstance(config["freshness"]["enabled_by_default"], bool):
        raise ValueError("freshness.enabled_by_default must be a boolean")
    return config


def resolve_for_ranker(config, ranker="graph_rerank"):
    """Return a validated copy with ranker-specific weights."""
    if ranker not in ("baseline", "rrf", "graph_rerank"):
        raise ValueError(f"Unknown ranker: {ranker}")
    resolved = copy.deepcopy(config)
    if ranker in ("baseline", "rrf"):
        resolved["weights"] = dict(_BASELINE_WEIGHTS)
        resolved["personalization"]["alpha"] = 0.0
    return validate_ranker_config(resolved, ranker)


def load_ranking_config(path=None):
    """Load and strictly validate config/ranking.json or the built-in default."""
    path = Path(path) if path is not None else constants.ROOT / "config" / "ranking.json"
    if path.exists():
        try:
            config = json.loads(path.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise ValueError(f"Invalid ranking config: {path}") from exc
    else:
        config = default_ranking_config()
    return validate_ranker_config(config)


def resolved_config_hash(config, ranker="graph_rerank"):
    return config_hash(resolve_for_ranker(config, ranker))
