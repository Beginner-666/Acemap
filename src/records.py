"""Acemap response envelopes and stable paper identity."""

from __future__ import annotations

import hashlib
import json
import re

from .text import normalize


def extract_records(payload):
    """Accept explicit common envelopes; never turn unknown JSON into no results."""
    if isinstance(payload, dict):
        if payload.get("success") is False:
            raise ValueError("Acemap 返回 success=false")
        if payload.get("error"):
            raise ValueError("Acemap 返回错误对象，请查看原始响应")

    def locate(value):
        if isinstance(value, list):
            return value
        if not isinstance(value, dict):
            return None
        if isinstance(value.get("hits"), dict):
            return locate(value["hits"])
        for key in ("data", "results", "items", "records", "list", "content", "works", "hits"):
            if key in value:
                found = locate(value[key])
                if found is not None:
                    return found
        return None

    records = locate(payload)
    if records is None:
        raise ValueError("尚不支持该接口响应结构；原始响应已缓存，需根据真实字段适配")
    normalized = []
    for item in records:
        if not isinstance(item, dict):
            raise ValueError("论文列表中包含非对象记录")
        record = item.get("_source", item)
        if not isinstance(record, dict):
            raise ValueError("论文 _source 不是对象")
        record = dict(record)
        if "_id" in item and "id" not in record:
            record["id"] = item["_id"]
        normalized.append(record)
    return normalized


def identity(record):
    """Build a deduplication key: DOI, then known IDs, then normalized title."""
    doi = record.get("doi")
    if doi:
        doi = re.sub(r"^(https?://(dx\.)?doi\.org/|doi:\s*)", "", str(doi), flags=re.I)
        return "doi:" + normalize(doi)
    for key in ("id", "work_id", "paper_id", "paperId", "workId"):
        if record.get(key) is not None:
            return key + ":" + str(record[key])
    title = record.get("title") or record.get("display_name") or record.get("name")
    if isinstance(title, str) and title.strip():
        return "title:" + normalize(title)
    # Unknown fields are preserved, but only identical records are merged.
    return "record:" + hashlib.sha256(json.dumps(record, sort_keys=True).encode()).hexdigest()
