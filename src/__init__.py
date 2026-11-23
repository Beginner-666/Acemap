"""Acemap search with query-time ConceptNet expansion.

The pipeline is intentionally split by responsibility:

- ``constants``: repository paths and service identifiers.
- ``jsonio``: JSON persistence and content hashing.
- ``text``: label normalization and ConceptNet URI parsing.
- ``edges``: edge ordering and one-hop neighbor ranking.
- ``net``: the single urllib surface used by network clients.
- ``records``: Acemap response parsing and paper identity.
- ``acemap_client``: cached Acemap HTTP access.
- ``conceptnet_index``: offline SQLite index construction.
- ``graph_index``: graph manifest, graph_id, read and validation contract.
- ``conceptnet_provider``: online/local ConceptNet neighbor lookup.
- ``cache_migration``: snapshot cache-key migration plan/apply/rollback.
- ``rankers``: ranker protocol plus frozen baseline/RRF implementations.
- ``config``: strict ranking configuration loading.
- ``paper_features``: read-only, fault-tolerant paper feature extraction.
- ``evaluation``: evaluation query, qrels, and config loaders.
- ``verification``: version-aware output validation and offline replay.
- ``versions``: frozen protocol version identifiers and hashing helpers.
- ``search``: query expansion, fusion, and result assembly.
- ``cli``: command-line parsing.
"""
