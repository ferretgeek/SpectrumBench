from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from stress_tool.engine_async import BenchmarkEngine, GlobalStats
from stress_tool.measurement import calculate_speed_measurement
from stress_tool.models import StressConfig
from stress_tool.prompts import MODE_CODEX, get_test_mode


def make_config(*, max_rounds: int = 2) -> StressConfig:
    mode = get_test_mode(MODE_CODEX)
    return StressConfig(
        api_key="test",
        base_url="http://127.0.0.1:8317/v1",
        model="gpt-5.5",
        test_mode=mode.key,
        reasoning_effort="high",
        system_prompt=mode.system_prompt,
        continue_prompt=mode.turn_prompts[0],
        max_rounds=max_rounds,
        request_interval_seconds=0.0,
        retry_seconds=0,
        num_workers=1,
        max_output_tokens=mode.max_output_tokens,
        warmup_rounds=1,
        input_price_per_million=5.0,
        output_price_per_million=30.0,
        cached_price_per_million=0.5,
        cache_write_price_per_million=5.0,
        pricing_model_id="gpt-5.5",
        service_tier="default",
    )


class FakeStream:
    def __init__(self, response: SimpleNamespace) -> None:
        self.response = response

    def __aiter__(self):
        return self._iterate()

    async def _iterate(self):
        yield SimpleNamespace(type="response.output_text.delta", delta="第一段可见文本。")
        await asyncio.sleep(0.055)
        yield SimpleNamespace(type="response.output_text.delta", delta="第二段可见文本，用于形成真实时间窗口。")
        yield SimpleNamespace(type="response.completed", response=self.response)


class FakeResponses:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        cached = 0 if len(self.calls) == 1 else 1152
        usage = SimpleNamespace(
            input_tokens=1320,
            output_tokens=100,
            total_tokens=1420,
            input_tokens_details=SimpleNamespace(cached_tokens=cached, cache_write_tokens=0),
            output_tokens_details=SimpleNamespace(reasoning_tokens=20),
        )
        response = SimpleNamespace(
            usage=usage,
            status="completed",
            service_tier="default",
            output_text="",
        )
        return FakeStream(response)


class FakeClient:
    def __init__(self) -> None:
        self.responses = FakeResponses()
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class EngineTests(unittest.IsolatedAsyncioTestCase):
    async def test_engine_uses_stable_cached_prompt_and_exact_speed(self) -> None:
        fake = FakeClient()
        events: list[tuple[str, object]] = []

        async def emit(event_type: str, data: object) -> None:
            events.append((event_type, data))

        engine = BenchmarkEngine()
        with patch("stress_tool.engine_async.AsyncOpenAI", return_value=fake):
            await engine.start(make_config(), emit)

        self.assertTrue(fake.closed)
        self.assertEqual(len(fake.responses.calls), 2)
        self.assertEqual(fake.responses.calls[0]["input"], fake.responses.calls[1]["input"])
        self.assertEqual(fake.responses.calls[0]["prompt_cache_key"], fake.responses.calls[1]["prompt_cache_key"])
        stats = engine.get_stats_snapshot()
        self.assertEqual(stats["measurement_version"], 3)
        self.assertEqual(stats["successful_requests"], 2)
        self.assertEqual(stats["warmup_requests"], 1)
        self.assertEqual(stats["formal_sample_count"], 1)
        self.assertEqual(stats["visible_output_tokens"], 160)
        self.assertEqual(stats["reasoning_tokens"], 40)
        self.assertGreater(stats["visible_tokens_per_second"], 0)
        self.assertEqual(stats["stop_reason"], "max_requests")
        self.assertTrue(any(event_type == "sample" for event_type, _ in events))

    async def test_interrupted_estimate_does_not_change_exact_totals(self) -> None:
        stats = GlobalStats(test_mode=MODE_CODEX)
        speed = calculate_speed_measurement(
            output_tokens=100,
            reasoning_tokens=20,
            request_started_at=0.0,
            first_visible_at=1.0,
            last_visible_at=2.0,
            request_completed_at=2.1,
            stream_text_chunks=8,
        )
        stats.add_success(
            1000,
            100,
            1100,
            2.1,
            cached=768,
            cache_write=0,
            reasoning=20,
            estimated_cost=0.01,
            output_chars=200,
            speed=speed,
            is_warmup=False,
            cache_detail_available=True,
            service_tier="default",
        )
        stats.add_interrupted_request(900, 50, 950, 0.02, latency=1.0)
        snapshot = stats.snapshot(stop_reason="manual_stop", pricing_available=True)
        self.assertEqual(snapshot["input_tokens"], 1000)
        self.assertEqual(snapshot["output_tokens"], 100)
        self.assertEqual(snapshot["total_tokens"], 1100)
        self.assertEqual(snapshot["partial_estimated_total_tokens"], 950)
        self.assertAlmostEqual(snapshot["estimated_cost"], 0.01)
        self.assertAlmostEqual(snapshot["partial_estimated_cost"], 0.02)

    async def test_missing_required_cache_write_detail_disables_cost(self) -> None:
        stats = GlobalStats(test_mode=MODE_CODEX)
        speed = calculate_speed_measurement(
            output_tokens=80,
            reasoning_tokens=16,
            request_started_at=0.0,
            first_visible_at=0.5,
            last_visible_at=1.5,
            request_completed_at=1.6,
            stream_text_chunks=4,
        )
        stats.add_success(
            1300,
            80,
            1380,
            1.6,
            cached=0,
            cache_write=0,
            reasoning=16,
            estimated_cost=0.01,
            output_chars=160,
            speed=speed,
            is_warmup=False,
            cache_detail_available=True,
            cache_write_detail_available=False,
            service_tier="default",
        )
        snapshot = stats.snapshot(stop_reason="completed", pricing_available=True)
        self.assertIsNone(snapshot["estimated_cost"])
        self.assertFalse(snapshot["pricing_details_valid"])
        self.assertTrue(any("cache_write_tokens" in reason for reason in snapshot["quality_reasons"]))


if __name__ == "__main__":
    unittest.main()
