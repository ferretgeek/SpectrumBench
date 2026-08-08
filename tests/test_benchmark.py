from __future__ import annotations

import unittest

from benchmark_token_speed import (
    EndpointConfig,
    build_comparisons,
    prepare_daily_messages,
)


class BenchmarkTests(unittest.TestCase):
    def test_daily_context_grows_then_resets_every_six_turns(self) -> None:
        conversation: list[dict[str, str]] = []
        first = prepare_daily_messages(conversation, 1)
        self.assertEqual([item["role"] for item in first], ["system", "user"])
        conversation.append({"role": "assistant", "content": "first answer"})
        second = prepare_daily_messages(conversation, 2)
        self.assertEqual([item["role"] for item in second], ["system", "user", "assistant", "user"])
        reset = prepare_daily_messages(conversation, 7)
        self.assertEqual([item["role"] for item in reset], ["system", "user"])

    def test_incompatible_models_do_not_publish_speed_ratio(self) -> None:
        endpoints = [
            EndpointConfig("a.txt", "https://a.example/v1", "x", "model-a"),
            EndpointConfig("b.txt", "https://b.example/v1", "x", "model-b"),
        ]
        rows = []
        for sequence in range(1, 4):
            for endpoint in endpoints:
                rows.append(
                    {
                        "endpoint": endpoint.label,
                        "sequence": sequence,
                        "model": endpoint.model,
                        "mode": "daily-dialogue",
                        "service_tier": "default",
                        "visible_tokens_per_second": 50.0,
                        "end_to_end_visible_tokens_per_second": 20.0,
                        "input_tokens": 100,
                        "cached_tokens": 0,
                    }
                )
        comparison = build_comparisons(rows, endpoints)["b.txt"]
        self.assertFalse(comparison["comparable"])
        self.assertIsNone(comparison["visible_speed_ratio"])
        self.assertIn("模型不同", comparison["comparability_notes"])


if __name__ == "__main__":
    unittest.main()
