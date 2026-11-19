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
- ``conceptnet_provider``: online/local ConceptNet neighbor lookup.
- ``search``: query expansion, fusion, and result assembly.
- ``cli``: command-line parsing.
"""
