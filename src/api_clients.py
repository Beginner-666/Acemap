"""Optional API adapters for the ``further`` demonstration branch.

The core search pipeline remains offline-first.  These adapters deliberately do
not contain credentials and are disabled by default.  A caller can fill
``config/api.example.json`` (or provide a private copy) and inject the
corresponding environment variable when online enrichment or DeepSeek RAG is
needed.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, quote
from urllib.request import Request, urlopen

from .jsonio import read_json


API_SCHEMA_VERSION = "api-config-v1"


class ApiConfigurationError(ValueError):
    """Raised when an optional provider is not configured safely."""


class ApiDisabled(ApiConfigurationError):
    """Raised when a caller requests a disabled provider."""


def load_api_config(path: str | Path) -> dict:
    """Load and validate the provider configuration without making requests."""
    value = read_json(path)
    if not isinstance(value, dict) or value.get("schema_version") != API_SCHEMA_VERSION:
        raise ApiConfigurationError(f"expected {API_SCHEMA_VERSION} configuration")
    providers = value.get("providers")
    if not isinstance(providers, dict):
        raise ApiConfigurationError("providers must be an object")
    for name, provider in providers.items():
        if not isinstance(provider, dict):
            raise ApiConfigurationError(f"provider {name} must be an object")
        if not isinstance(provider.get("enabled", False), bool):
            raise ApiConfigurationError(f"provider {name}.enabled must be boolean")
        timeout = provider.get("timeout_seconds", 20)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
            raise ApiConfigurationError(f"provider {name}.timeout_seconds must be positive")
    return value


def provider_status(config: dict, environment: dict[str, str] | None = None) -> dict:
    """Return a safe status summary; secrets are never included."""
    environment = os.environ if environment is None else environment
    result = {}
    for name, provider in config.get("providers", {}).items():
        key_name = str(provider.get("api_key_env") or "")
        result[name] = {
            "enabled": bool(provider.get("enabled")),
            "configured": bool(provider.get("enabled"))
            and bool(provider.get("base_url"))
            and (not key_name or bool(environment.get(key_name))),
            "base_url_set": bool(provider.get("base_url")),
            "api_key_env": key_name or None,
        }
    return result


def _provider(config: dict, name: str) -> dict:
    provider = config.get("providers", {}).get(name)
    if not isinstance(provider, dict) or not provider.get("enabled"):
        raise ApiDisabled(f"provider {name} is disabled")
    if not provider.get("base_url"):
        raise ApiConfigurationError(f"provider {name} has no base_url")
    return provider


def request_json(url: str, *, method: str = "GET", payload: dict | None = None,
                headers: dict[str, str] | None = None, timeout: float = 20,
                retries: int = 1) -> dict:
    """Small standard-library JSON client used only by optional adapters."""
    body = None
    request_headers = {"Accept": "application/json", "User-Agent": "ScholarLink/1.0"}
    request_headers.update(headers or {})
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request_headers.setdefault("Content-Type", "application/json")
    last_error = None
    for attempt in range(max(0, retries) + 1):
        try:
            request = Request(url, data=body, headers=request_headers, method=method)
            with urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8-sig"))
        except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
            last_error = error
            if attempt >= max(0, retries):
                break
    raise ApiConfigurationError(f"API request failed: {url}: {last_error}")


class OpenAlexClient:
    """Optional OpenAlex metadata and citation enrichment adapter."""

    def __init__(self, config: dict):
        self.provider = _provider(config, "openalex")

    def _url(self, path: str, params: dict | None = None) -> str:
        url = self.provider["base_url"].rstrip("/") + "/" + path.lstrip("/")
        params = dict(params or {})
        mailto = self.provider.get("mailto")
        if mailto:
            params["mailto"] = mailto
        return url + ("?" + urlencode(params) if params else "")

    def search(self, query: str, per_page: int = 10) -> dict:
        return request_json(
            self._url("works", {"search": query, "per-page": per_page}),
            timeout=float(self.provider.get("timeout_seconds", 20)),
            retries=int(self.provider.get("retries", 1)),
        )

    def work(self, identifier: str) -> dict:
        encoded = quote(identifier, safe="")
        return request_json(
            self._url("works/" + encoded),
            timeout=float(self.provider.get("timeout_seconds", 20)),
            retries=int(self.provider.get("retries", 1)),
        )


class DeepSeekClient:
    """OpenAI-compatible DeepSeek chat adapter with no embedded secret."""

    def __init__(self, config: dict, environment: dict[str, str] | None = None):
        self.provider = _provider(config, "deepseek")
        self.environment = os.environ if environment is None else environment
        key_name = self.provider.get("api_key_env", "DEEPSEEK_API_KEY")
        self.api_key = self.environment.get(key_name, "")
        if not self.api_key:
            raise ApiConfigurationError(f"missing environment variable: {key_name}")

    def complete(self, messages: list[dict[str, str]], *, model: str | None = None) -> dict:
        path = self.provider.get("chat_path", "/chat/completions")
        url = self.provider["base_url"].rstrip("/") + "/" + path.lstrip("/")
        payload: dict[str, Any] = {
            "model": model or self.provider.get("model", "deepseek-chat"),
            "messages": messages,
            "temperature": float(self.provider.get("temperature", 0)),
            "stream": False,
        }
        return request_json(
            url,
            method="POST",
            payload=payload,
            headers={"Authorization": "Bearer " + self.api_key},
            timeout=float(self.provider.get("timeout_seconds", 30)),
            retries=int(self.provider.get("retries", 1)),
        )


def build_deepseek_messages(query: str, evidence: dict) -> list[dict[str, str]]:
    """Build a bounded evidence-first prompt for a future DeepSeek call."""
    context = json.dumps(evidence, ensure_ascii=False, separators=(",", ":"))
    return [
        {
            "role": "system",
            "content": (
                "You are ScholarLink. Answer only from the supplied evidence. "
                "Every factual claim must cite an allowed paper or graph_fact id. "
                "If evidence is insufficient, say so explicitly. Return concise JSON "
                "with a claims array containing text and citations."
            ),
        },
        {"role": "user", "content": f"Question: {query}\nEvidence: {context}"},
    ]
