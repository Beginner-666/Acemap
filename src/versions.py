"""Frozen interface version identifiers and canonical hashing helpers.

These constants are the single source of truth for the version namespaces
described in ``docs/interface_protocol.md``.  Do not reuse one version number
for another namespace.
"""

from __future__ import annotations

import hashlib
import json

PROTOCOL_VERSION = 1

OUTPUT_SCHEMA_V2 = 2
OUTPUT_SCHEMA_V3 = 3

INDEX_SCHEMA_VERSION = 1

SNAPSHOT_KEY_SCHEMA = 2

GRAPH_RULES_VERSION = "raw-v1"
CLEAN_GRAPH_RULES_VERSION = "clean-v1"

RANKER_PROTOCOL_VERSION = 1
RECORD_FEATURE_VERSION = 1
VERIFIER_VERSION = 2

CONCEPTNET_ONLINE_SOURCE_ID = "online:conceptnet-api:limit=1000:v1"
MISSING_GRAPH_ID = "g-missing"


def canonical_json(value):
    """Return the canonical JSON used for hashes and identifiers.

    The format is UTF-8, sorted keys, compact separators, no trailing newline.
    """
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(value):
    """Return the lowercase hex SHA-256 of a UTF-8 string."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def config_hash(ranker_config):
    """Return the frozen v3 ranker-config hash."""
    return "sha256:" + sha256_text("ranker-config-v1\0" + canonical_json(ranker_config))
