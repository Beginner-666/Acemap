"""Read-only, fault-tolerant paper feature extraction.

The raw Acemap record is preserved in search output; this module only creates
a stable view for ranking and evidence.  See docs/interface_protocol.md §8.
"""

from __future__ import annotations

import math

from .records import normalize_doi
from .text import normalize


def _text(value):
    return value if isinstance(value, str) else ""


def _optional_text(value):
    return value if isinstance(value, str) and value else None


def _integer(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value) and value.is_integer():
        return int(value)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(float(value)) else None


def _year(record):
    year = _integer(record.get("publication_year"))
    if year is not None:
        return year
    date = record.get("publication_date")
    if isinstance(date, str) and len(date) >= 4 and date[:4].isdigit():
        return int(date[:4])
    return None


def _dict_items(value):
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _dedupe(items, key):
    seen = set()
    result = []
    for item in items:
        token = key(item)
        if token in seen:
            continue
        seen.add(token)
        result.append(item)
    return result


def _keywords(value):
    if not isinstance(value, list):
        return []
    values = [item for item in value if isinstance(item, str) and item.strip()]
    return _dedupe(values, lambda item: normalize(item))


def _concepts(value):
    concepts = []
    for item in _dict_items(value):
        concepts.append({
            "id": _optional_text(item.get("id")),
            "display_name": _text(item.get("display_name")),
            "score": _number(item.get("score")),
            "level": _integer(item.get("level")),
            "wikidata": _optional_text(item.get("wikidata")),
        })
    return _dedupe(concepts, lambda item: item["id"] or normalize(item["display_name"]))


def _topic_name(value):
    return _text(value.get("display_name")) if isinstance(value, dict) else ""


def _topics(value):
    topics = []
    for item in _dict_items(value):
        topics.append({
            "display_name": _text(item.get("display_name")),
            "field": _topic_name(item.get("field")),
            "subfield": _topic_name(item.get("subfield")),
            "domain": _topic_name(item.get("domain")),
        })
    return _dedupe(topics, lambda item: normalize(item["display_name"]))


def _authorships(record):
    return _dict_items(record.get("authorships"))


def _authors(authorships):
    authors = []
    for authorship in authorships:
        author = authorship.get("author")
        if not isinstance(author, dict):
            continue
        authors.append({
            "id": _optional_text(author.get("id")),
            "display_name": _text(author.get("display_name")
                                  or authorship.get("raw_author_name")),
            "orcid": _optional_text(author.get("orcid")),
            "author_position": _optional_text(authorship.get("author_position")),
        })
    return _dedupe(authors, lambda item: item["id"] or normalize(item["display_name"]))


def _institutions(authorships):
    institutions = []
    for authorship in authorships:
        for institution in _dict_items(authorship.get("institutions")):
            lineage = institution.get("lineage")
            institutions.append({
                "id": _optional_text(institution.get("id")),
                "display_name": _text(institution.get("display_name")),
                "ror": _optional_text(institution.get("ror")),
                "country_code": _optional_text(institution.get("country_code")),
                "type": _optional_text(institution.get("type")),
                "lineage": ([item for item in lineage if isinstance(item, str)]
                            if isinstance(lineage, list) else []),
            })
    return _dedupe(institutions,
                   lambda item: item["id"] or normalize(item["display_name"]))


def _venue(record):
    location = record.get("primary_location")
    if not isinstance(location, dict):
        return None
    source = location.get("source")
    if not isinstance(source, dict):
        return None
    return _optional_text(source.get("display_name"))


def paper_features(record):
    """Return the frozen feature view for one Acemap record.

    Missing or malformed fields degrade to empty strings/lists/None; this
    function never raises for an ordinary mapping.
    """
    if not isinstance(record, dict):
        record = {}
    authorships = _authorships(record)
    doi = record.get("doi")
    return {
        "title": (_text(record.get("title")) or _text(record.get("display_name"))
                  or _text(record.get("name"))),
        "abstract": _text(record.get("abstract")),
        "publication_date": _optional_text(record.get("publication_date")),
        "year": _year(record),
        "doi": normalize_doi(doi) if doi else None,
        "keywords": _keywords(record.get("keywords")),
        "concepts": _concepts(record.get("concepts")),
        "topics": _topics(record.get("topics")),
        "authors": _authors(authorships),
        "institutions": _institutions(authorships),
        "venue": _venue(record),
        "cited_by_count": _integer(record.get("cited_by_count")),
    }
