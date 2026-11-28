"""Acemap search client with on-disk response replay."""

from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

from . import net
from .constants import ENDPOINT, USER_AGENT
from .jsonio import read_json, write_json
from .records import extract_records


class AcemapClient:
    def __init__(self, cache_dir, timeout, retries, cache_only, refresh, *, persist=True):
        self.cache_dir = Path(cache_dir)
        self.timeout = timeout
        self.retries = retries
        self.cache_only = cache_only
        self.refresh = refresh
        self.persist = persist

    def search(self, keyword, size):
        return self._search_params(
            {"keyword": keyword, "order": "desc", "page": 1, "size": size})

    def search_filtered(self, keyword, size, author_ids=None, institution_ids=None):
        """Search with Acemap author/institution filters (phase-one multi-hop).

        ``author_ids`` and ``institution_ids`` are the raw Acemap identifiers
        found in first-round paper authorships.  The original response and
        request parameters are cached exactly like an ordinary search.
        """
        params = {"keyword": keyword, "order": "desc", "page": 1, "size": size}
        if author_ids:
            params["author_ids"] = ",".join(sorted(dict.fromkeys(author_ids)))
        if institution_ids:
            params["institution_ids"] = ",".join(sorted(dict.fromkeys(institution_ids)))
        return self._search_params(params)

    def _search_params(self, params):
        url = ENDPOINT + "?" + urlencode(params)
        key = hashlib.sha256(url.encode("utf-8")).hexdigest()
        path = self.cache_dir / f"{key}.json"
        if path.exists() and not self.refresh:
            snapshot = read_json(path)
            if snapshot.get("url") != url:
                raise ValueError("缓存请求信息不匹配")
        else:
            if self.cache_only:
                raise ValueError(f"缺少离线响应缓存：{path}")
            raw = self.fetch(url)
            snapshot = {"url": url, "parameters": params,
                        "retrieved_at": datetime.now(timezone.utc).isoformat(),
                        "raw_response": raw}
            # Save the original body before decoding or adapting its schema.
            if self.persist:
                write_json(path, snapshot)
        payload = json.loads(snapshot["raw_response"])
        return extract_records(payload), path.name if self.persist else None

    def fetch(self, url):
        for attempt in range(self.retries + 1):
            try:
                request = net.Request(url, headers={"User-Agent": USER_AGENT,
                                                    "Accept": "application/json"})
                with net.urlopen(request, timeout=self.timeout) as response:
                    return response.read().decode("utf-8-sig")
            except net.HTTPError as exc:
                if exc.code not in (429, 500, 502, 503, 504) or attempt == self.retries:
                    raise ValueError(f"Acemap HTTP {exc.code}：{url}") from exc
            except (net.URLError, TimeoutError, OSError) as exc:
                if attempt == self.retries:
                    raise ValueError(f"Acemap 网络请求失败：{exc}") from exc
            time.sleep(min(2 ** attempt, 8))
        raise ValueError("Acemap 请求未完成")
