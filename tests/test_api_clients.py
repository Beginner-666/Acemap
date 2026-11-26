import json
import os
import tempfile
import unittest
from pathlib import Path

from src.api_clients import (
    API_SCHEMA_VERSION,
    ApiConfigurationError,
    ApiDisabled,
    DeepSeekClient,
    build_deepseek_messages,
    load_api_config,
    provider_status,
)


class ApiClientTests(unittest.TestCase):
    def config(self, **overrides):
        value = {
            "schema_version": API_SCHEMA_VERSION,
            "providers": {
                "openalex": {"enabled": False, "base_url": "", "timeout_seconds": 20},
                "deepseek": {"enabled": False, "base_url": "", "timeout_seconds": 30,
                             "api_key_env": "DEEPSEEK_API_KEY"},
            },
        }
        for name, values in overrides.items():
            value["providers"][name].update(values)
        return value

    def write_config(self, value):
        handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                             encoding="utf-8")
        with handle:
            json.dump(value, handle)
        self.addCleanup(lambda: Path(handle.name).unlink(missing_ok=True))
        return handle.name

    def test_disabled_provider_is_safe_and_never_configured(self):
        config = self.config()
        self.assertEqual(provider_status(config)["deepseek"]["configured"], False)
        with self.assertRaises(ApiDisabled):
            DeepSeekClient(config, environment={})

    def test_enabled_deepseek_requires_environment_key(self):
        config = self.config(deepseek={"enabled": True, "base_url": "https://example.test"})
        with self.assertRaises(ApiConfigurationError):
            DeepSeekClient(config, environment={})
        client = DeepSeekClient(config, environment={"DEEPSEEK_API_KEY": "placeholder"})
        self.assertEqual(client.provider["base_url"], "https://example.test")

    def test_config_file_and_prompt_are_deterministic(self):
        path = self.write_config(self.config())
        loaded = load_api_config(path)
        self.assertEqual(loaded["schema_version"], API_SCHEMA_VERSION)
        messages = build_deepseek_messages(
            "What is robotics?", {"papers": [{"id": "paper-1"}], "graph_facts": []})
        self.assertEqual(messages[0]["role"], "system")
        self.assertIn("paper-1", messages[1]["content"])

    def test_invalid_schema_is_rejected(self):
        path = self.write_config({"schema_version": "wrong", "providers": {}})
        with self.assertRaises(ApiConfigurationError):
            load_api_config(path)


if __name__ == "__main__":
    unittest.main()
