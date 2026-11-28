"""Text normalization and ConceptNet URI parsing."""

from __future__ import annotations

from urllib.parse import unquote


def normalize(text):
    """Case-fold, unify underscores, and collapse whitespace."""
    return " ".join(str(text).casefold().replace("_", " ").split())


def concept(uri, language):
    """Extract a normalized label from ``/c/<language>/<label>[/...]`` URIs.

    Optional POS or sense suffixes are ignored so that seed and query labels
    match the surface form reported by ConceptNet.
    """
    parts = uri.split("/")
    if len(parts) < 4 or parts[1:3] != ["c", language]:
        return None
    return normalize(unquote(parts[3]))
