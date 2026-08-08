from __future__ import annotations

import unittest
from dataclasses import replace

import tiktoken

from stress_tool.engine_async import BenchmarkEngine
from stress_tool.pricing import load_pricing_table, resolve_model_pricing
from stress_tool.prompts import (
    MODE_CODEX,
    MODE_DAILY,
    MODE_UNCACHED,
    TEST_MODES,
    cache_buster,
    get_test_mode,
)
from stress_tool.server import (
    _build_config,
    _config_to_frontend_payload,
    _normalize_base_url,
    _safe_error_text,
    _sanitize_frontend_config_payload,
    _sanitize_history_entry,
)


class ModesAndConfigTests(unittest.TestCase):
    def test_exactly_three_locked_single_stream_modes(self) -> None:
        self.assertEqual(list(TEST_MODES), [MODE_UNCACHED, MODE_DAILY, MODE_CODEX])
        for key, mode in TEST_MODES.items():
            config = _build_config(
                {
                    "api_key": "test",
                    "base_url": "https://example.test/v1",
                    "model": "gpt-5.6-sol",
                    "test_mode": key,
                    "reasoning_effort": mode.default_reasoning,
                }
            )
            self.assertEqual(config.num_workers, 1)
            self.assertEqual(config.formal_max_rounds, mode.default_max_requests)
            self.assertEqual(config.max_rounds, mode.default_max_requests + mode.warmup_rounds)

    def test_codex_fixed_prefix_exceeds_cache_threshold(self) -> None:
        mode = get_test_mode(MODE_CODEX)
        encoding = tiktoken.get_encoding("o200k_base")
        self.assertGreaterEqual(len(encoding.encode(mode.system_prompt)), 1024)

    def test_cache_buster_has_stable_token_count_and_changes_content(self) -> None:
        encoding = tiktoken.get_encoding("o200k_base")
        values = [cache_buster(f"seed-{index}") for index in range(8)]
        self.assertEqual(len(set(values)), len(values))
        self.assertEqual(len({len(encoding.encode(value)) for value in values}), 1)

    def test_legacy_high_concurrency_and_prompts_are_ignored(self) -> None:
        config = _build_config(
            {
                "api_key": "test",
                "base_url": "http://127.0.0.1:8317/v1",
                "model": "gpt-5.5",
                "test_mode": MODE_CODEX,
                "reasoning_effort": "high",
                "num_workers": "100",
                "system_prompt": "legacy",
                "continue_prompt": "legacy",
            }
        )
        self.assertEqual(config.num_workers, 1)
        self.assertEqual(config.formal_max_rounds, get_test_mode(MODE_CODEX).default_max_requests)
        self.assertEqual(config.max_rounds, config.formal_max_rounds + config.warmup_rounds)
        self.assertNotEqual(config.system_prompt, "legacy")
        self.assertTrue(config.pricing_available)
        self.assertEqual(config.input_price_per_million, 5.0)

        public = _config_to_frontend_payload(config)
        self.assertNotIn("api_key", public)
        self.assertNotIn("base_url", public)
        self.assertEqual(public["mode_version"], 3)
        self.assertEqual(public["max_requests"], str(config.formal_max_rounds))

        with self.assertRaisesRegex(ValueError, "单流串行"):
            replace(config, num_workers=2)

    def test_incompatible_reasoning_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "只允许推理等级"):
            _build_config(
                {
                    "api_key": "test",
                    "base_url": "http://127.0.0.1:8317/v1",
                    "model": "gpt-5.5",
                    "test_mode": MODE_CODEX,
                    "reasoning_effort": "medium",
                }
            )

    def test_persisted_payload_drops_old_measurement_controls(self) -> None:
        cleaned = _sanitize_frontend_config_payload(
            {
                "api_key": "secret",
                "model": "gpt-5.5",
                "num_workers": "100",
                "system_prompt": "old",
                "model_pricing": {"x": {}},
            }
        )
        self.assertNotIn("api_key", cleaned)
        self.assertNotIn("base_url", cleaned)
        self.assertNotIn("num_workers", cleaned)
        self.assertNotIn("system_prompt", cleaned)
        self.assertNotIn("model_pricing", cleaned)

    def test_history_and_errors_never_expose_credentials(self) -> None:
        cleaned = _sanitize_history_entry(
            {
                "config": {
                    "api_key": "sk-example-secret",
                    "base_url": "https://private.example/v1",
                    "model": "gpt-5.5",
                    "mode_version": 3,
                },
                "stats": {},
            }
        )
        assert cleaned is not None
        self.assertNotIn("api_key", cleaned["config"])
        self.assertNotIn("base_url", cleaned["config"])
        self.assertEqual(cleaned["config"]["mode_version"], 3)

        embedded_url = "".join(("https", "://", "user:password", "@", "example.test/v1?api_key=query-secret"))
        safe = _safe_error_text(RuntimeError(f"Bearer private-token sk-example-secret proj_example {embedded_url}"))
        self.assertNotIn("private-token", safe)
        self.assertNotIn("sk-example-secret", safe)
        self.assertNotIn("proj_example", safe)
        self.assertNotIn("password", safe)
        self.assertNotIn("query-secret", safe)

        exact_key = "opaque-private-value"
        exact_url = "https://private-host.example/v1"
        exact_safe = _safe_error_text(
            RuntimeError(f"failed at {exact_url}/responses with {exact_key}"),
            exact_key,
            exact_url,
        )
        self.assertNotIn(exact_key, exact_safe)
        self.assertNotIn("private-host", exact_safe)

    def test_base_url_rejects_remote_http_and_embedded_identity(self) -> None:
        self.assertEqual(
            _normalize_base_url("http://127.0.0.1:8317"),
            "http://127.0.0.1:8317/v1",
        )
        with self.assertRaisesRegex(ValueError, "必须使用 HTTPS"):
            _normalize_base_url("http://api.example.test/v1")
        with self.assertRaisesRegex(ValueError, "用户名或密码"):
            _normalize_base_url("".join(("https", "://", "user:pass", "@", "example.test/v1")))
        with self.assertRaisesRegex(ValueError, "查询参数"):
            _normalize_base_url("https://example.test/v1?token=secret")

    def test_pricing_resolves_date_alias(self) -> None:
        pricing = resolve_model_pricing("gpt-5.6-terra-2026-07-01", load_pricing_table())
        self.assertIsNotNone(pricing)
        assert pricing is not None
        self.assertEqual(pricing["model_id"], "gpt-5.6-terra")
        self.assertEqual(pricing["input"], 2.0)
        self.assertEqual(pricing["cached_input"], 0.2)
        self.assertEqual(pricing["cache_write"], 2.5)
        self.assertEqual(pricing["output"], 12.0)

    def test_pricing_resolves_public_sol_alias(self) -> None:
        pricing = resolve_model_pricing("gpt-5.6", load_pricing_table())
        self.assertIsNotNone(pricing)
        assert pricing is not None
        self.assertEqual(pricing["model_id"], "gpt-5.6-sol")

    def test_gpt_56_pricing_requires_cache_write_details(self) -> None:
        config = _build_config(
            {
                "api_key": "test",
                "base_url": "http://127.0.0.1:8317/v1",
                "model": "gpt-5.6",
                "test_mode": MODE_CODEX,
                "reasoning_effort": "high",
            }
        )
        self.assertTrue(config.cache_write_details_required)

    def test_terminal_errors_do_not_retry(self) -> None:
        quota = BenchmarkEngine._classify_exception(
            RuntimeError("You exceeded your current quota, please check your plan")
        )
        invalid = BenchmarkEngine._classify_exception(
            RuntimeError("Project `proj_example` does not have access to model `x`")
        )
        transient = BenchmarkEngine._classify_exception(RuntimeError("429 rate limit exceeded"))
        self.assertEqual(quota[1:], ("quota_exhausted", False))
        self.assertEqual(invalid[1:], ("invalid_request", False))
        self.assertEqual(transient[1:], ("rate_limit", True))
        self.assertNotIn("proj_example", invalid[0])


if __name__ == "__main__":
    unittest.main()
