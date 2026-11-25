"""Deterministic, rule-based query understanding for the v3 search pipeline.

The module is pure: it never reads caches, performs network calls, writes files,
or observes wall-clock time.  Version ``qu-v1`` is frozen by the phase-one
implementation; changing the rules requires a new version and new replay
fixtures.  See docs/interface_protocol.md §4.9.
"""

from __future__ import annotations

import re

from .edges import edge_order
from .text import normalize

QUERY_UNDERSTANDING_VERSION = "qu-v1"

# Relation priority follows the phase-one requirement: lexical variants first,
# then ISA/part-of structure, then loose association.
RELATION_PRIORITY = {
    "/r/Synonym": 0,
    "/r/FormOf": 1,
    "/r/DerivedFrom": 2,
    "/r/IsA": 3,
    "/r/PartOf": 4,
    "/r/HasA": 5,
    "/r/RelatedTo": 6,
}

# Conservative generic-node filter.  These labels are broad enough to drift
# into unrelated domains when used as expansion terms.
GENERIC_TERMS = frozenset({
    "act", "action", "activity", "anything", "area", "areas", "bad", "cause",
    "different", "effect", "entity", "entities", "everything", "example",
    "examples", "first", "good", "group", "groups", "item", "items", "kind",
    "kinds", "large", "last", "new", "object", "objects", "old", "part",
    "parts", "people", "person", "place", "places", "process", "processes",
    "related", "similar", "small", "something", "state", "states", "thing",
    "things", "time", "times", "type", "types", "way", "ways",
})

STOPWORDS = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "in",
    "into", "is", "of", "on", "or", "the", "to", "with",
})

TIME_WORDS = frozenset({"recent", "recently", "latest", "new", "newest",
                        "current", "contemporary", "modern"})
TASK_WORDS = frozenset({"research", "review", "reviews", "survey", "surveys",
                        "study", "studies", "paper", "papers", "article",
                        "articles", "literature"})
CONNECTORS = frozenset({"about", "for", "in", "of", "on", "regarding", "the"})
YEAR_RANGE = re.compile(
    r"\b((?:19|20)\d{2})\s*(?:-\s*|to\s+)?((?:19|20)\d{2})\b")


def normalize_query(text):
    """Normalize case, underscores, hyphens and whitespace for matching.

    The ``query`` field in search output keeps the frozen ``text.normalize``
    form; this helper is only used for intent parsing and candidate matching.
    """
    return " ".join(str(text).casefold().replace("_", " ").replace("-", " ").split())


def singular_forms(word):
    """Return the word plus a conservative English singular candidate."""
    word = word.casefold()
    forms = [word]
    if len(word) > 4 and word.endswith("ies"):
        forms.append(word[:-3] + "y")
    elif len(word) > 3 and word.endswith(("ses", "xes", "zes", "ches", "shes")):
        forms.append(word[:-2])
    elif (len(word) > 3 and word.endswith("s") and not word.endswith(("ss", "us", "is"))):
        forms.append(word[:-1])
    return list(dict.fromkeys(forms))


def _query_forms(query):
    forms = []
    normalized = normalize_query(query)
    for token in normalized.split():
        for form in singular_forms(token):
            forms.append(form)
    forms.append(normalized)
    return list(dict.fromkeys(forms))


def parse_intent(query):
    """Split a query into topic, optional time filter and task qualifier.

    Only the first rule-based form is supported: ``<time> <task> on <topic>``.
    If no qualifier is recognized the whole normalized query is the topic and
    ``parsed`` is false; callers must keep searching with the original term.
    """
    normalized = normalize_query(query)
    time_filter = None
    range_match = YEAR_RANGE.search(normalized)
    if range_match:
        time_filter = {"start": int(range_match.group(1)),
                       "end": int(range_match.group(2))}
        tokens = (normalized[:range_match.start()].split()
                  + normalized[range_match.end():].split())
    else:
        tokens = normalized.split()
    task = None
    removed = set()
    qualifiers = set()
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token in TIME_WORDS:
            time_filter = "recent"
            removed.add(index)
            qualifiers.add(index)
            index += 1
            continue
        if token in TASK_WORDS:
            task = token
            removed.add(index)
            qualifiers.add(index)
            index += 1
            continue
        # Explicit year forms: since 2020, before 2022.
        if token.isdigit() and len(token) == 4:
            year = int(token)
            previous = tokens[index - 1] if index > 0 else ""
            if previous in ("since", "after", "from"):
                time_filter = {"start": year, "end": None}
                removed.update({index - 1, index})
                qualifiers.update({index - 1, index})
                index += 1
                continue
            if previous in ("before", "until"):
                time_filter = {"start": None, "end": year}
                removed.update({index - 1, index})
                qualifiers.update({index - 1, index})
                index += 1
                continue
        index += 1
    # Connectors belong to the qualifier syntax only when adjacent to a
    # qualifier or at the query edge; keep "in medicine" in the topic.
    for position, token in enumerate(tokens):
        if token in CONNECTORS and (position == 0 or position == len(tokens) - 1
                                    or position - 1 in qualifiers
                                    or position + 1 in qualifiers):
            removed.add(position)
    topic_tokens = [token for position, token in enumerate(tokens)
                    if position not in removed]
    topic = " ".join(topic_tokens)
    if not topic:
        return {"query": normalized, "topic": normalized, "time_filter": None,
                "task": None, "parsed": False, "version": QUERY_UNDERSTANDING_VERSION}
    return {"query": normalized, "topic": topic, "time_filter": time_filter,
            "task": task, "parsed": bool(removed),
            "version": QUERY_UNDERSTANDING_VERSION}


def _neighbor(edge, query_forms):
    subject = normalize_query(edge["subject"])
    obj = normalize_query(edge["object"])
    if subject in query_forms and obj not in query_forms:
        return edge["object"]
    if obj in query_forms and subject not in query_forms:
        return edge["subject"]
    return None


def _morphology(term):
    return " ".join(singular_forms(token)[-1] for token in normalize_query(term).split())


def _degree(edge_list):
    degree = {}
    for edge in edge_list:
        for label in (edge["subject"], edge["object"]):
            degree[label] = degree.get(label, 0) + 1
    return degree


def select_expansions(edge_list, query, limit, min_edge_weight=1,
                      degree_cap=None, relation_whitelist=None):
    """Select filtered, provenance-preserving one-hop expansion candidates.

    The returned objects keep the frozen expansion shape:
    ``term``, ``evidence`` and ``domain_support_seeds``.  The evidence object
    itself carries relation type, weight and both URIs, so path length one is
    fully traceable without adding fields.

    ``qu-v1`` ordering: descending edge weight (confidence), then relation
    priority (Synonym/FormOf/DerivedFrom/IsA/PartOf/HasA/RelatedTo), then the
    stable edge keys.  Morphological duplicates of the query are removed.
    """
    if not isinstance(limit, int) or limit < 1 or not query:
        return []
    whitelist = (set(RELATION_PRIORITY) if relation_whitelist is None
                 else set(relation_whitelist))
    query_forms = set(_query_forms(query))
    query_forms.add(normalize(query))
    query_morphology = _morphology(query)
    degree = _degree(edge_list)
    candidates = {}
    for edge in edge_list:
        if edge.get("relation") not in whitelist:
            continue
        if edge.get("weight", 0) < min_edge_weight:
            continue
        neighbor = _neighbor(edge, query_forms)
        if not neighbor:
            continue
        term = normalize(neighbor)
        if not term or term in query_forms:
            continue
        tokens = term.split()
        if len(term) < 2 or len(tokens) > 6:
            continue
        meaningful = [token for token in tokens if token not in STOPWORDS]
        if not meaningful or term in GENERIC_TERMS:
            continue
        if _morphology(term) == query_morphology:
            continue
        if degree_cap is not None and degree.get(neighbor, 0) > degree_cap:
            continue
        existing = candidates.get(term)
        if existing is None or edge_order(edge) < edge_order(existing):
            candidates[term] = edge
    ordered = sorted(candidates.items(), key=lambda pair: (
        -pair[1]["weight"],
        RELATION_PRIORITY.get(pair[1]["relation"], len(RELATION_PRIORITY)),
        pair[1]["assertion"], pair[1]["subject_uri"], pair[1]["object_uri"],
        pair[0]))
    return [{"term": term, "evidence": edge, "domain_support_seeds": []}
            for term, edge in ordered[:limit]]
