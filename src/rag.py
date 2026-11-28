"""Evidence-first, offline RAG assembly.

The default generator is extractive and deterministic.  Applications may pass
an optional callable generator, but every generated claim is still required to
carry a citation to a graph assertion or a paper.  Generator failures degrade
to the same evidence-only answer instead of hiding the retrieved evidence.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable

from .jsonio import digest, read_json, write_json
from .paper_features import paper_features
from .records import identity
from .text import normalize


RAG_SCHEMA_VERSION = "rag-v1"
_TOKEN_RE = re.compile(r"[a-z0-9]+", re.I)


def _text(value, limit=600):
    value = value if isinstance(value, str) else ""
    value = " ".join(value.split())
    return value[:limit]


def _safe_fact(fact):
    if not isinstance(fact, dict):
        return None
    subject = _text(fact.get("subject"), 200)
    relation = _text(fact.get("relation"), 120)
    obj = _text(fact.get("object"), 200)
    source = _text(fact.get("source") or fact.get("assertion"), 240)
    if not subject or not relation or not obj:
        return None
    if not source:
        source = f"graph:{normalize(subject)}|{relation}|{normalize(obj)}"
    return {"subject": subject, "relation": relation, "object": obj, "source": source}


def _facts_from_search(row, limit):
    facts = []
    for edge in (row.get("conceptnet", {}).get("edges", [])
                 if isinstance(row.get("conceptnet"), dict) else []):
        fact = _safe_fact(edge)
        if fact is not None:
            facts.append(fact)
    for result in row.get("results", []):
        for path in result.get("evidence", {}).get("graph_paths", []):
            nodes = path.get("nodes", [])
            relations = path.get("relations", [])
            assertions = path.get("assertions", [])
            if len(nodes) >= 2 and relations:
                fact = _safe_fact({"subject": nodes[0], "relation": relations[0],
                                   "object": nodes[-1],
                                   "assertion": assertions[0] if assertions else ""})
                if fact is not None:
                    facts.append(fact)
    seen = set()
    result = []
    for fact in facts:
        key = (normalize(fact["subject"]), fact["relation"], normalize(fact["object"]),
               fact["source"])
        if key not in seen:
            seen.add(key)
            result.append(fact)
        if len(result) >= limit:
            break
    return result


def _paper_item(result, citation):
    paper = result.get("paper") if isinstance(result, dict) else result
    if not isinstance(paper, dict):
        return None
    features = paper_features(paper)
    title = _text(features["title"], 300)
    abstract = _text(features["abstract"], 900)
    if not title and not abstract:
        return None
    return {"id": identity(paper), "title": title, "doi": features["doi"],
            "year": features["year"], "snippet": abstract,
            "citation": citation}


def build_context(query: str, search_document: dict | None = None,
                  graph_facts: Iterable[dict] | None = None,
                  papers: Iterable[dict] | None = None, max_facts: int = 12,
                  max_papers: int = 8) -> dict:
    """Build a sanitized, bounded evidence context from a v2/v3 search file."""
    if not isinstance(query, str) or not query.strip():
        raise ValueError("query must be non-empty")
    if isinstance(max_facts, bool) or max_facts < 0 or max_papers < 0:
        raise ValueError("context limits must be non-negative integers")
    rows = []
    if isinstance(search_document, dict):
        rows = [row for row in search_document.get("searches", [])
                if isinstance(row, dict) and normalize(row.get("query", "")) == normalize(query)]
        if not rows:
            rows = [row for row in search_document.get("searches", [])
                    if isinstance(row, dict)]
    facts = []
    for fact in graph_facts or []:
        safe = _safe_fact(fact)
        if safe is not None:
            facts.append(safe)
    for row in rows:
        facts.extend(_facts_from_search(row, max_facts))
    unique_facts = []
    seen = set()
    for fact in facts:
        key = (normalize(fact["subject"]), fact["relation"], normalize(fact["object"]), fact["source"])
        if key not in seen:
            seen.add(key)
            unique_facts.append(fact)
        if len(unique_facts) >= max_facts:
            break

    paper_values = list(papers or [])
    if not paper_values:
        for row in rows:
            for index, result in enumerate(row.get("results", []), 1):
                item = _paper_item(result, f"paper-{index}")
                if item is not None:
                    paper_values.append(item)
                if len(paper_values) >= max_papers:
                    break
    safe_papers = []
    paper_seen = set()
    for index, item in enumerate(paper_values[:max_papers], 1):
        if isinstance(item, dict) and "paper" in item:
            item = _paper_item(item, item.get("citation") or f"paper-{index}")
        elif isinstance(item, dict):
            item = {"id": _text(item.get("id"), 200), "title": _text(item.get("title"), 300),
                    "doi": item.get("doi"), "year": item.get("year"),
                    "snippet": _text(item.get("snippet"), 900),
                    "citation": item.get("citation") or f"paper-{index}"}
        if item is None or not item.get("title"):
            continue
        if item["id"] in paper_seen:
            continue
        paper_seen.add(item["id"])
        safe_papers.append(item)
    return {"query": " ".join(query.split()), "graph_facts": unique_facts,
            "papers": safe_papers}


def _sentence(text):
    text = _text(text, 500)
    if not text:
        return ""
    return text if text.endswith((".", "!", "?")) else text + "."


def extractive_answer(context: dict) -> dict:
    """Create a deterministic answer whose every bullet has evidence."""
    facts = context.get("graph_facts", [])
    papers = context.get("papers", [])
    claims = []
    for index, fact in enumerate(facts[:5], 1):
        claims.append({"text": _sentence(
            f"{fact['subject']} {fact['relation']} {fact['object']}"),
            "citations": [fact["source"] or f"graph-{index}"], "type": "graph"})
    for paper in papers[:5]:
        detail = paper.get("snippet") or "No abstract was provided."
        year = f" ({paper['year']})" if paper.get("year") else ""
        claims.append({"text": _sentence(f"{paper['title']}{year}: {detail}"),
                       "citations": [paper["citation"]], "type": "paper"})
    if not claims:
        claims = [{"text": "Insufficient evidence was retrieved to answer this question.",
                   "citations": [], "type": "insufficient_evidence"}]
    answer = "\n".join(f"- {claim['text']} [{', '.join(claim['citations'])}]"
                        if claim["citations"] else f"- {claim['text']}"
                        for claim in claims)
    insufficient = any(claim["type"] == "insufficient_evidence" for claim in claims)
    return {"text": answer, "claims": claims,
            "grounded": bool(claims) and not insufficient and all(
                bool(claim["citations"]) for claim in claims),
            "insufficient_evidence": insufficient}


def generate(context: dict, generator: Callable[[dict], dict | str] | None = None) -> dict:
    """Use an optional generator and fall back to extractive evidence."""
    fallback = extractive_answer(context)
    if generator is None:
        return {"mode": "extractive", "model": "none", "prompt_version": "rag-v1",
                "answer": fallback, "error": None}
    try:
        generated = generator(context)
        if isinstance(generated, str):
            raise ValueError("generator must return structured claims with citations")
        if not isinstance(generated, dict) or not isinstance(generated.get("claims"), list):
            raise ValueError("generator must return an answer with claims")
        allowed = {fact.get("source") for fact in context.get("graph_facts", []) if fact.get("source")}
        allowed.update(paper.get("citation") for paper in context.get("papers", [])
                       if paper.get("citation"))
        allowed.update(paper.get("id") for paper in context.get("papers", []) if paper.get("id"))
        if any(not isinstance(claim, dict) or not claim.get("citations")
               for claim in generated["claims"]):
            raise ValueError("every generated claim must contain citations")
        if any(any(citation not in allowed for citation in claim.get("citations", []))
               for claim in generated["claims"]):
            raise ValueError("generated claims contain citations outside the evidence context")
        generated.setdefault("grounded", True)
        generated.setdefault("insufficient_evidence", False)
        return {"mode": "generated", "model": "custom", "prompt_version": "rag-v1",
                "answer": generated, "error": None}
    except Exception as error:  # generator is an optional, untrusted boundary
        return {"mode": "extractive_fallback", "model": "none",
                "prompt_version": "rag-v1", "answer": fallback, "error": str(error)}


def build_document(context: dict, generated: dict, source: str | dict | None = None) -> dict:
    return {"schema_version": RAG_SCHEMA_VERSION, "source": source,
            "query": context["query"], "graph_facts": context["graph_facts"],
            "papers": context["papers"], "answer": generated["answer"],
            "generation": {"mode": generated["mode"], "model": generated["model"],
                            "prompt_version": generated["prompt_version"],
                            "error": generated["error"]},
            "quality": {"grounded": bool(generated["answer"].get("grounded")),
                        "insufficient_evidence": bool(
                            generated["answer"].get("insufficient_evidence")),
                        "citation_count": sum(len(claim.get("citations", []))
                                               for claim in generated["answer"].get("claims", []))}}


def validate_document(document: dict) -> dict:
    if not isinstance(document, dict) or document.get("schema_version") != RAG_SCHEMA_VERSION:
        raise ValueError("invalid rag-v1 document")
    for key in ("query", "graph_facts", "papers", "answer", "generation", "quality"):
        if key not in document:
            raise ValueError(f"RAG document missing {key}")
    answer = document["answer"]
    if not isinstance(answer, dict) or not isinstance(answer.get("claims"), list):
        raise ValueError("RAG answer must contain claims")
    for claim in answer["claims"]:
        if not isinstance(claim, dict) or not isinstance(claim.get("text"), str):
            raise ValueError("invalid RAG claim")
        if claim.get("type") != "insufficient_evidence" and not claim.get("citations"):
            raise ValueError("RAG claim is missing citations")
    allowed = {fact.get("source") for fact in document["graph_facts"] if fact.get("source")}
    allowed.update(paper.get("citation") for paper in document["papers"]
                   if paper.get("citation"))
    allowed.update(paper.get("id") for paper in document["papers"] if paper.get("id"))
    for claim in answer["claims"]:
        if any(citation not in allowed for citation in claim.get("citations", [])):
            raise ValueError("RAG claim citation is not present in evidence")
    return document


def run_rag(args):
    document = read_json(args.results) if args.results else None
    context = build_context(args.query, search_document=document,
                            max_facts=args.max_facts, max_papers=args.max_papers)
    generated = generate(context)
    source = ({"file": args.results.name, "sha256": digest(args.results)}
              if args.results else None)
    output = build_document(context, generated, source=source)
    validate_document(output)
    write_json(args.output, output)
    return 0
