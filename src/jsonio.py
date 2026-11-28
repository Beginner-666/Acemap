"""JSON persistence and content hashing with portable LF newlines."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


def read_json(path):
    # Accept UTF-8 files produced by both Python and PowerShell.  Writing
    # remains BOM-free so frozen output bytes stay unchanged.
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def write_json(path, value):
    """Write atomically with deterministic formatting and LF line endings."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n",
                         encoding="utf-8", newline="\n")
    temporary.replace(path)


def digest(path):
    result = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()
