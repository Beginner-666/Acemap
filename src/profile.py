"""Explicit-consent user profile and deterministic personalization.

Profiles are built from records the caller explicitly supplies.  No network,
wall-clock time, or hidden user data is consulted.  The output is independent
from v2/v3 search documents so the frozen search artifacts remain unchanged.
"""

from __future__ import annotations

import math
import re
from pathlib import Path

from .jsonio import digest, read_json, write_json
from .paper_features import paper_features
from .records import identity
from .text import normalize


PROFILE_SCHEMA_VERSION = "personalized-v1"
PROFILE_VERSION = "profile-v1"
_YEAR_RE = re.compile(r"(19|20)\d{2}")
_KINDS = ("authors", "institutions", "concepts", "topics", "keywords")


def _year(value):
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    match = _YEAR_RE.search(str(value or ""))
    return int(match.group()) if match else None


def _event_record(event):
    if not isinstance(event, dict):
        return None, None
    if isinstance(event.get("record"), dict):
        return event["record"], event.get("timestamp") or event.get("year")
    if isinstance(event.get("paper"), dict):
        return event["paper"], event.get("timestamp") or event.get("year")
    if any(key in event for key in ("title", "display_name", "doi", "id", "authorships")):
        return event, event.get("timestamp") or event.get("year")
    return None, None


def _history_records(history):
    if isinstance(history, dict):
        if isinstance(history.get("history"), list):
            history = history["history"]
        elif isinstance(history.get("searches"), list):
            events = []
            for row in history["searches"]:
                for result in row.get("results", []) if isinstance(row, dict) else []:
                    if isinstance(result, dict):
                        events.append({"record": result.get("paper", result),
                                       "timestamp": result.get("timestamp")})
            history = events
    if not isinstance(history, list):
        raise ValueError("history must be a list or an object containing history/searches")
    result = []
    for event in history:
        if isinstance(event, dict) and event.get("consent") is False:
            continue
        record, timestamp = _event_record(event)
        if record is not None:
            result.append((record, timestamp))
    return result


def _item_key(item):
    return normalize(item.get("id") or item.get("display_name") or item.get("name") or "")


def _features_for_profile(features):
    values = {kind: [] for kind in _KINDS}
    values["authors"] = [item for item in features["authors"] if _item_key(item)]
    values["institutions"] = [item for item in features["institutions"] if _item_key(item)]
    values["concepts"] = [item for item in features["concepts"] if _item_key(item)]
    values["topics"] = [item for item in features["topics"] if _item_key(item)]
    values["keywords"] = [{"id": keyword, "display_name": keyword}
                           for keyword in features["keywords"] if normalize(keyword)]
    return values


def build_profile(history, consent: bool = False, reference_year: int = 2025,
                  half_life_years: float = 5.0, max_events: int = 100) -> dict:
    """Build a weighted interest profile from explicitly authorized history."""
    if not consent:
        return {"version": PROFILE_VERSION, "enabled": False,
                "reason": "explicit consent was not provided", "events": 0,
                "reference_year": reference_year, "half_life_years": half_life_years,
                "features": {kind: [] for kind in _KINDS}}
    if isinstance(reference_year, bool) or not isinstance(reference_year, int):
        raise ValueError("reference_year must be an integer")
    if isinstance(half_life_years, bool) or not isinstance(half_life_years, (int, float)) \
            or not math.isfinite(float(half_life_years)) or half_life_years <= 0:
        raise ValueError("half_life_years must be positive and finite")
    if isinstance(max_events, bool) or not isinstance(max_events, int) or max_events < 1:
        raise ValueError("max_events must be >= 1")
    totals = {kind: {} for kind in _KINDS}
    labels = {kind: {} for kind in _KINDS}
    records = _history_records(history)[:max_events]
    for record, timestamp in records:
        year = _year(timestamp) or paper_features(record)["year"]
        age = max(0, reference_year - year) if year is not None else 0
        weight = 0.5 ** (age / float(half_life_years))
        values = _features_for_profile(paper_features(record))
        for kind, items in values.items():
            for item in items:
                key = _item_key(item)
                if not key:
                    continue
                totals[kind][key] = totals[kind].get(key, 0.0) + weight
                labels[kind].setdefault(key, item.get("display_name") or item.get("id") or key)
    features = {}
    for kind in _KINDS:
        values = sorted(totals[kind].items(), key=lambda item: (-item[1], item[0]))
        maximum = values[0][1] if values else 0.0
        features[kind] = [{"key": key, "label": labels[kind][key],
                           "weight": round(value / maximum, 12) if maximum else 0.0}
                          for key, value in values[:100]]
    return {"version": PROFILE_VERSION, "enabled": True, "reason": None,
            "events": len(records), "reference_year": reference_year,
            "half_life_years": float(half_life_years), "features": features}


def _profile_lookup(profile):
    lookup = {kind: {} for kind in _KINDS}
    if not isinstance(profile, dict) or not profile.get("enabled"):
        return lookup
    for kind in _KINDS:
        for item in profile.get("features", {}).get(kind, []):
            key = normalize(item.get("key", "")) if isinstance(item, dict) else ""
            if key:
                lookup[kind][key] = float(item.get("weight", 0.0))
    return lookup


def score_record(record: dict, profile: dict) -> tuple[float, list[dict]]:
    """Return a [0, 1] interest score and explainable matches."""
    lookup = _profile_lookup(profile)
    if not any(lookup.values()):
        return 0.0, []
    values = _features_for_profile(paper_features(record))
    matches = []
    total = 0.0
    for kind, items in values.items():
        for item in items:
            key = _item_key(item)
            weight = lookup[kind].get(key)
            if weight is not None:
                total += weight
                matches.append({"type": kind[:-1] if kind.endswith("s") else kind,
                                "key": key, "label": item.get("display_name") or key,
                                "weight": weight})
    # A document containing several matching fields should approach one but
    # cannot exceed it; the denominator is the maximum possible contribution
    # across feature types, rather than the whole profile vocabulary.
    denominator = sum(max(values_for_kind.values(), default=0.0)
                     for values_for_kind in lookup.values())
    score = min(1.0, total / denominator) if denominator else 0.0
    matches.sort(key=lambda item: (-item["weight"], item["type"], item["key"]))
    return score, matches


def personalize_results(results: list[dict], profile: dict, alpha: float = 0.2) -> list[dict]:
    if isinstance(alpha, bool) or not isinstance(alpha, (int, float)) \
            or not math.isfinite(float(alpha)) or not 0 <= alpha <= 1:
        raise ValueError("alpha must be a number in [0, 1]")
    enabled = bool(profile.get("enabled")) and bool(profile.get("events"))
    effective_alpha = float(alpha) if enabled else 0.0
    base_values = []
    for item in results:
        value = item.get("score", item.get("fusion_score", 0.0))
        try:
            base_values.append(max(0.0, float(value)))
        except (TypeError, ValueError):
            base_values.append(0.0)
    maximum = max(base_values, default=0.0)
    output = []
    for item, raw_base in zip(results, base_values):
        base = raw_base / maximum if maximum else 0.0
        score, matches = score_record(item.get("paper", {}), profile) if enabled else (0.0, [])
        final = (1.0 - effective_alpha) * base + effective_alpha * score
        output.append({"key": item.get("key") or identity(item.get("paper", {})),
                       "paper": item.get("paper", {}), "base_score": base,
                       "profile_score": score, "score": final,
                       "profile_matches": matches,
                       "first_round": bool(item.get("first_round", True))})
    return sorted(output, key=lambda item: (-item["score"], item["key"]))


def build_document(search_document: dict, profile: dict, alpha: float,
                   source: str | None = None) -> dict:
    if isinstance(alpha, bool) or not isinstance(alpha, (int, float)) \
            or not math.isfinite(float(alpha)) or not 0 <= alpha <= 1:
        raise ValueError("alpha must be a number in [0, 1]")
    searches = []
    for row in search_document.get("searches", []):
        results = personalize_results(row.get("results", []), profile, alpha)
        searches.append({"query": row.get("query", ""), "status": row.get("status", "ok"),
                         "results": results})
    return {"schema_version": PROFILE_SCHEMA_VERSION, "source": source,
            "profile": profile, "settings": {"alpha": float(alpha)},
            "summary": {"queries": len(searches),
                        "personalization_applied": bool(profile.get("enabled") and profile.get("events")),
                        "results": sum(len(row["results"]) for row in searches)},
            "searches": searches}


def validate_document(document: dict) -> dict:
    if not isinstance(document, dict) or document.get("schema_version") != PROFILE_SCHEMA_VERSION:
        raise ValueError("invalid personalized-v1 document")
    for key in ("profile", "settings", "summary", "searches"):
        if key not in document:
            raise ValueError(f"personalized document missing {key}")
    alpha = document["settings"].get("alpha")
    if isinstance(alpha, bool) or not isinstance(alpha, (int, float)) or not 0 <= alpha <= 1:
        raise ValueError("personalized alpha must be in [0, 1]")
    for row in document["searches"]:
        previous = None
        for item in row.get("results", []):
            for key in ("base_score", "profile_score", "score"):
                value = item.get(key)
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1:
                    raise ValueError(f"invalid personalized score: {key}")
            order = (-item["score"], item["key"])
            if previous is not None and order < previous:
                raise ValueError("personalized results are not sorted")
            expected = (1.0 - alpha) * item["base_score"] + alpha * item["profile_score"] \
                if document["profile"].get("enabled") and document["profile"].get("events") \
                else item["base_score"]
            if not math.isclose(item["score"], expected, rel_tol=0, abs_tol=1e-12):
                raise ValueError("personalized score does not match its components")
            previous = order
    return document


def run_personalize(args):
    search_path = Path(args.results)
    search_document = read_json(search_path)
    history = read_json(args.history) if args.history else []
    profile = build_profile(history, consent=args.consent,
                            reference_year=args.reference_year,
                            half_life_years=args.half_life_years,
                            max_events=args.max_events)
    output = build_document(search_document, profile, args.alpha, source=search_path.name)
    validate_document(output)
    write_json(args.output, output)
    return 0


def run_profile(args):
    history = read_json(args.history)
    profile = build_profile(history, consent=args.consent,
                            reference_year=args.reference_year,
                            half_life_years=args.half_life_years,
                            max_events=args.max_events)
    write_json(args.output, profile)
    return 0
