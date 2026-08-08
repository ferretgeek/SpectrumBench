"""Single-stream Responses API benchmark engine.

Requests are deliberately serialized so quota accounting, cache behavior and
token-speed samples remain attributable and comparable between runs.
"""

from __future__ import annotations

import asyncio
import inspect
import math
import re
import secrets
import time
from collections import deque
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

import tiktoken
from openai import AsyncOpenAI

from stress_tool.measurement import (
    MEASUREMENT_VERSION,
    SpeedMeasurement,
    calculate_speed_measurement,
    summarize,
)
from stress_tool.models import StressConfig
from stress_tool.prompts import (
    MODE_DAILY,
    MODE_UNCACHED,
    cache_buster,
    daily_turn_prompt,
    get_test_mode,
    is_daily_cycle_start,
    prompt_cache_key,
)

PREVIEW_CHAR_LIMIT = 220
REQUEST_TIMEOUT_SECONDS = 120.0
STREAM_IDLE_TIMEOUT_SECONDS = 45.0
RATE_LIMIT_KEYWORDS = ("rate_limit", "rate limit", "429", "too many")
NETWORK_KEYWORDS = ("timeout", "timed out", "connection", "idle")
AUTH_KEYWORDS = ("authentication", "unauthorized", "invalid api key", "401")
CONTEXT_LIMIT_KEYWORDS = (
    "context_length",
    "max_tokens",
    "too long",
    "context window",
    "maximum context",
    "token limit",
    "context_length_exceeded",
)
QUOTA_EXHAUSTED_KEYWORDS = (
    "exceeded your current quota",
    "insufficient_quota",
    "quota_exceeded",
    "quota exhausted",
    "usage limit reached",
    "weekly limit",
    "monthly limit",
    "plan limit",
    "额度已用尽",
    "配额已用尽",
)
INVALID_REQUEST_KEYWORDS = (
    "does not have access to model",
    "model_not_found",
    "unsupported model",
    "invalid_request_error",
    "unknown parameter",
)
STATS_THROTTLE_SECONDS = 1.0
LOG_BATCH_SECONDS = 0.25
LOG_BATCH_SIZE = 200
MAX_LOG_BUFFER = 4000
MAX_RECORDS = 10000
MAX_LATENCY_SAMPLES = 10000
RECENT_OUTPUT_RATE_WINDOW_SECONDS = 12.0
SPEED_SAMPLE_WINDOW_SECONDS = 60.0
LIVE_RATE_WINDOW_SECONDS = 2.4
LIVE_TOKEN_ESTIMATE_TAIL_CHARS = 96
# Only stop a worker after prolonged continuous rate limiting.
# This keeps short-lived upstream turbulence from terminating the run too early.
MAX_TRANSIENT_RETRIES = 5
MAX_CONTEXT_RETRIES = 2
MAX_OTHER_RETRIES = 1

EventCallback = Callable[[str, Any], Awaitable[None]]


def _append_bounded_sample(samples: list[int], value: int) -> None:
    if len(samples) >= MAX_LATENCY_SAMPLES:
        del samples[: len(samples) - MAX_LATENCY_SAMPLES + 1]
    samples.append(max(int(value), 0))


def _percentile_ms(values: list[int], percentile: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(len(ordered) * percentile) - 1))
    return ordered[index]


def _append_bounded_float(samples: list[float], value: float) -> None:
    if len(samples) >= MAX_LATENCY_SAMPLES:
        del samples[: len(samples) - MAX_LATENCY_SAMPLES + 1]
    samples.append(max(float(value), 0.0))


@dataclass
class GlobalStats:
    test_mode: str = MODE_DAILY
    successful_requests: int = 0
    failed_requests: int = 0
    failed_rate_limit_requests: int = 0
    failed_network_requests: int = 0
    failed_context_requests: int = 0
    failed_auth_requests: int = 0
    failed_quota_requests: int = 0
    failed_invalid_requests: int = 0
    failed_interrupted_requests: int = 0
    failed_other_requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    visible_output_tokens: int = 0
    total_tokens: int = 0
    cached_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0
    uncached_input_tokens: int = 0
    regular_input_tokens: int = 0
    estimated_cost: float = 0.0
    partial_estimated_input_tokens: int = 0
    partial_estimated_output_tokens: int = 0
    partial_estimated_cost: float = 0.0
    successful_rounds: int = 0
    failed_rounds: int = 0
    exact_token_requests: int = 0
    interrupted_token_requests: int = 0
    warmup_requests: int = 0
    cache_detail_missing_requests: int = 0
    cache_write_detail_missing_requests: int = 0
    output_chars_completed: int = 0
    total_latency: float = 0.0
    all_request_latency: float = 0.0
    timed_request_count: int = 0
    successful_latency_samples_ms: list[int] = field(default_factory=list)
    all_latency_samples_ms: list[int] = field(default_factory=list)
    first_token_latency_samples_ms: list[int] = field(default_factory=list)
    visible_tps_samples: list[float] = field(default_factory=list)
    e2e_visible_tps_samples: list[float] = field(default_factory=list)
    billed_output_tps_samples: list[float] = field(default_factory=list)
    per_request_cost_samples: list[float] = field(default_factory=list)
    per_request_total_token_samples: list[int] = field(default_factory=list)
    speed_excluded_samples: int = 0
    measurement_input_tokens: int = 0
    measurement_cached_tokens: int = 0
    measurement_cache_write_tokens: int = 0
    service_tiers: set[str] = field(default_factory=set)
    started_at: float = field(default_factory=time.time)

    def add_success(
        self,
        inp: int,
        out: int,
        total: int,
        latency: float,
        *,
        cached: int,
        cache_write: int,
        reasoning: int,
        estimated_cost: float,
        output_chars: int,
        speed: SpeedMeasurement,
        is_warmup: bool,
        cache_detail_available: bool,
        cache_write_detail_available: bool = True,
        service_tier: str = "",
    ) -> None:
        inp = max(int(inp), 0)
        out = max(int(out), 0)
        total = max(int(total), inp + out)
        cached = max(0, min(int(cached), inp))
        cache_write = max(0, min(int(cache_write), inp - cached))
        reasoning = max(0, min(int(reasoning), out))
        cost = float(estimated_cost or 0.0)
        if not math.isfinite(cost):
            cost = 0.0

        self.successful_requests += 1
        self.successful_rounds += 1
        self.exact_token_requests += 1
        self.input_tokens += inp
        self.output_tokens += out
        self.visible_output_tokens += max(out - reasoning, 0)
        self.total_tokens += total
        self.cached_tokens += cached
        self.cache_write_tokens += cache_write
        self.reasoning_tokens += reasoning
        self.uncached_input_tokens += max(inp - cached, 0)
        self.regular_input_tokens += max(inp - cached - cache_write, 0)
        self.estimated_cost += cost
        self.output_chars_completed += max(int(output_chars), 0)
        self.total_latency += max(float(latency), 0.0)
        self.all_request_latency += max(float(latency), 0.0)
        self.timed_request_count += 1
        latency_ms = round(max(float(latency), 0.0) * 1000)
        _append_bounded_sample(self.successful_latency_samples_ms, latency_ms)
        _append_bounded_sample(self.all_latency_samples_ms, latency_ms)
        if not cache_detail_available:
            self.cache_detail_missing_requests += 1
        if not cache_write_detail_available:
            self.cache_write_detail_missing_requests += 1
        if service_tier:
            self.service_tiers.add(str(service_tier))

        if is_warmup:
            self.warmup_requests += 1
            return

        self.measurement_input_tokens += inp
        self.measurement_cached_tokens += cached
        self.measurement_cache_write_tokens += cache_write
        _append_bounded_sample(self.per_request_total_token_samples, total)
        if cost > 0:
            _append_bounded_float(self.per_request_cost_samples, cost)
        if speed.ttft_seconds > 0:
            _append_bounded_sample(self.first_token_latency_samples_ms, round(speed.ttft_seconds * 1000))
        if speed.end_to_end_visible_tokens_per_second > 0:
            _append_bounded_float(self.e2e_visible_tps_samples, speed.end_to_end_visible_tokens_per_second)
        if speed.billed_output_tokens_per_second > 0:
            _append_bounded_float(self.billed_output_tps_samples, speed.billed_output_tokens_per_second)
        if speed.speed_valid:
            _append_bounded_float(self.visible_tps_samples, speed.visible_tokens_per_second)
        else:
            self.speed_excluded_samples += 1

    def add_failed_request(self, reason: str = "other", latency: float = 0.0) -> None:
        self.failed_requests += 1
        self.failed_rounds += 1
        self._bump_failure_reason(reason)
        if latency > 0:
            self.all_request_latency += max(float(latency), 0.0)
            self.timed_request_count += 1
            _append_bounded_sample(self.all_latency_samples_ms, round(max(float(latency), 0.0) * 1000))

    def add_interrupted_request(
        self,
        inp: int,
        out: int,
        total: int,
        estimated_cost: float = 0.0,
        output_chars: int = 0,
        cached: int = 0,
        reasoning: int = 0,
        latency: float = 0.0,
    ) -> None:
        del output_chars, cached, reasoning
        self.failed_requests += 1
        self.failed_rounds += 1
        self.failed_interrupted_requests += 1
        self.interrupted_token_requests += 1
        self.partial_estimated_input_tokens += max(int(inp), 0)
        self.partial_estimated_output_tokens += max(int(out), 0)
        cost = float(estimated_cost or 0.0)
        self.partial_estimated_cost += cost if math.isfinite(cost) else 0.0
        if latency > 0:
            self.all_request_latency += max(float(latency), 0.0)
            self.timed_request_count += 1
            _append_bounded_sample(self.all_latency_samples_ms, round(max(float(latency), 0.0) * 1000))

    def _bump_failure_reason(self, reason: str) -> None:
        normalized = (reason or "other").strip().lower()
        mapping = {
            "rate_limit": "failed_rate_limit_requests",
            "network": "failed_network_requests",
            "context": "failed_context_requests",
            "auth": "failed_auth_requests",
            "quota_exhausted": "failed_quota_requests",
            "invalid_request": "failed_invalid_requests",
        }
        attr = mapping.get(normalized, "failed_other_requests")
        setattr(self, attr, getattr(self, attr) + 1)

    def snapshot(
        self,
        live_input_tokens_hint: int = 0,
        live_output_tokens_estimate_hint: int = 0,
        live_output_tps_hint: float = 0.0,
        live_cost_hint: float = 0.0,
        live_billable_input_tokens_hint: int = 0,
        inflight_requests_hint: int = 0,
        active_workers_hint: int = 0,
        waiting_workers_hint: int = 0,
        settled_token_source: str = "exact",
        live_token_source: str = "none",
        tokenizer_name: str = "",
        stop_reason: str = "running",
        pricing_available: bool = False,
    ) -> dict[str, Any]:
        now = time.time()
        elapsed = max(now - self.started_at, 0.1)
        settled = self.successful_requests + self.failed_requests
        inflight = max(int(inflight_requests_hint), 0)
        formal_samples = max(self.successful_requests - self.warmup_requests, 0)

        speed_summary = summarize(self.visible_tps_samples, digits=2)
        e2e_summary = summarize(self.e2e_visible_tps_samples, digits=2)
        billed_summary = summarize(self.billed_output_tps_samples, digits=2)
        ttft_summary = summarize(self.first_token_latency_samples_ms, digits=0)
        token_summary = summarize(self.per_request_total_token_samples, digits=2)
        cost_summary = summarize(self.per_request_cost_samples, digits=8)
        cache_ratio = self.cached_tokens / self.input_tokens if self.input_tokens > 0 else 0.0
        measured_cache_ratio = (
            self.measurement_cached_tokens / self.measurement_input_tokens if self.measurement_input_tokens > 0 else 0.0
        )
        cache_write_ratio = self.cache_write_tokens / self.input_tokens if self.input_tokens > 0 else 0.0

        mode = get_test_mode(self.test_mode)
        cache_status = "observed"
        cache_valid: bool | None = None
        if formal_samples <= 0:
            cache_status = "insufficient"
        elif self.cache_detail_missing_requests > 0:
            cache_status = "unavailable"
            cache_valid = False
        elif mode.cache_policy == "none":
            cache_valid = measured_cache_ratio <= mode.cache_ratio_max
            cache_status = "valid" if cache_valid else "failed"
        elif mode.cache_policy == "high":
            if formal_samples < 2:
                cache_status = "insufficient"
            else:
                cache_valid = measured_cache_ratio >= mode.cache_ratio_min
                cache_status = "valid" if cache_valid else "failed"

        quality_reasons: list[str] = []
        if formal_samples < 3:
            quality_reasons.append("正式样本少于 3 条")
        if int(speed_summary["count"]) < 3:
            quality_reasons.append("有效速度样本少于 3 条")
        if cache_valid is False:
            quality_reasons.append("缓存实测未达到当前模式要求")
        if float(speed_summary["cv"]) > 0.15 and int(speed_summary["count"]) >= 3:
            quality_reasons.append("可见输出速度 CV 高于 15%")
        if self.cache_detail_missing_requests > 0:
            quality_reasons.append("部分响应缺少 cached_tokens 明细")
        pricing_tier_valid = not self.service_tiers or self.service_tiers.issubset({"default"})
        pricing_details_valid = self.cache_write_detail_missing_requests == 0
        if pricing_available and not pricing_tier_valid:
            quality_reasons.append("实际服务层级不是 default，标准档美元换算已停用")
        if pricing_available and not pricing_details_valid:
            quality_reasons.append("响应缺少 cache_write_tokens，美元换算已停用")
        if formal_samples < 3 or int(speed_summary["count"]) < 3:
            quality_status = "insufficient"
        elif quality_reasons:
            quality_status = "warning"
        else:
            quality_status = "valid"

        live_output = max(int(round(live_output_tokens_estimate_hint)), 0)
        display_input = self.input_tokens + max(int(live_input_tokens_hint), 0)
        display_output = self.output_tokens + live_output
        display_cost = self.estimated_cost + max(float(live_cost_hint), 0.0)
        effective_pricing_available = bool(pricing_available and pricing_tier_valid and pricing_details_valid)
        avg_success_latency = self.total_latency / self.successful_requests if self.successful_requests else 0.0
        avg_all_latency = self.all_request_latency / self.timed_request_count if self.timed_request_count else 0.0
        primary_tps = float(speed_summary["median"])
        return {
            "measurement_version": MEASUREMENT_VERSION,
            "test_mode": self.test_mode,
            "request_count": settled + inflight,
            "settled_request_count": settled,
            "inflight_request_count": inflight,
            "active_workers": max(int(active_workers_hint), 0),
            "waiting_workers": max(int(waiting_workers_hint), 0),
            "effective_concurrency": max(max(int(active_workers_hint), 0), inflight),
            "round_count": self.successful_rounds + self.failed_rounds,
            "successful_requests": self.successful_requests,
            "failed_requests": self.failed_requests,
            "failed_rate_limit_requests": self.failed_rate_limit_requests,
            "failed_network_requests": self.failed_network_requests,
            "failed_context_requests": self.failed_context_requests,
            "failed_auth_requests": self.failed_auth_requests,
            "failed_quota_requests": self.failed_quota_requests,
            "failed_invalid_requests": self.failed_invalid_requests,
            "failed_interrupted_requests": self.failed_interrupted_requests,
            "failed_other_requests": self.failed_other_requests,
            "interrupted_requests": self.failed_interrupted_requests,
            "exact_token_requests": self.exact_token_requests,
            "estimated_token_requests": 0,
            "interrupted_token_requests": self.interrupted_token_requests,
            "warmup_requests": self.warmup_requests,
            "formal_sample_count": formal_samples,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "visible_output_tokens": self.visible_output_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "total_tokens": self.total_tokens,
            "cached_tokens": self.cached_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "uncached_input_tokens": self.uncached_input_tokens,
            "regular_input_tokens": self.regular_input_tokens,
            "billable_input_tokens": self.uncached_input_tokens,
            "cache_read_ratio": round(cache_ratio, 4),
            "measurement_cache_read_ratio": round(measured_cache_ratio, 4),
            "cache_write_ratio": round(cache_write_ratio, 4),
            "cache_validation_status": cache_status,
            "cache_validation_passed": cache_valid,
            "cache_detail_missing_requests": self.cache_detail_missing_requests,
            "cache_write_detail_missing_requests": self.cache_write_detail_missing_requests,
            "partial_estimated_input_tokens": self.partial_estimated_input_tokens,
            "partial_estimated_output_tokens": self.partial_estimated_output_tokens,
            "partial_estimated_total_tokens": self.partial_estimated_input_tokens
            + self.partial_estimated_output_tokens,
            "partial_estimated_cost": round(self.partial_estimated_cost, 6),
            "display_input_tokens": display_input,
            "display_output_tokens": display_output,
            "display_total_tokens": display_input + display_output,
            "live_input_delta_tokens": max(display_input - self.input_tokens, 0),
            "live_output_delta_tokens": max(display_output - self.output_tokens, 0),
            "live_total_delta_tokens": max(display_input + display_output - self.total_tokens, 0),
            "display_billable_input_tokens": self.uncached_input_tokens + max(int(live_billable_input_tokens_hint), 0),
            "live_billable_input_delta_tokens": max(int(live_billable_input_tokens_hint), 0),
            "pricing_available": effective_pricing_available,
            "pricing_tier_valid": pricing_tier_valid,
            "pricing_details_valid": pricing_details_valid,
            "estimated_cost": round(self.estimated_cost, 6) if effective_pricing_available else None,
            "display_estimated_cost": round(display_cost, 6) if effective_pricing_available else None,
            "live_estimated_cost_delta": round(max(display_cost - self.estimated_cost, 0.0), 6),
            "per_request_cost": cost_summary,
            "per_request_total_tokens": token_summary,
            "per_msg_cost_sample_count": int(cost_summary["count"]),
            "per_msg_cost_median": round(float(cost_summary["median"]), 6),
            "per_msg_cost_p25": round(float(cost_summary["p25"]), 6),
            "per_msg_cost_p75": round(float(cost_summary["p75"]), 6),
            "per_msg_cost_mean": round(float(cost_summary["mean"]), 6),
            "per_msg_token_mean": round(float(token_summary["mean"]), 1),
            "per_msg_token_stdev": round(float(token_summary["stdev"]), 1),
            "per_msg_token_cv": round(float(token_summary["cv"]), 4),
            "visible_tps": speed_summary,
            "end_to_end_visible_tps": e2e_summary,
            "billed_output_tps": billed_summary,
            "ttft_ms": ttft_summary,
            "speed_sample_count": int(speed_summary["count"]),
            "speed_excluded_sample_count": self.speed_excluded_samples,
            "speed_exact_sample_count": int(speed_summary["count"]),
            "speed_estimated_sample_count": 0,
            "visible_tokens_per_second": primary_tps,
            "end_to_end_visible_tokens_per_second": float(e2e_summary["median"]),
            "billed_output_tokens_per_second": float(billed_summary["median"]),
            "live_visible_tokens_per_second_estimate": round(max(float(live_output_tps_hint), 0.0), 2),
            "aggregate_visible_tokens_per_second": round(self.visible_output_tokens / elapsed, 2),
            "tokens_per_second": primary_tps,
            "recent_tokens_per_second": primary_tps,
            "average_tokens_per_second": round(self.visible_output_tokens / elapsed, 2),
            "speed_output_tokens_per_second": primary_tps,
            "speed_recent_tokens_per_second": primary_tps,
            "speed_tps_p50": primary_tps,
            "speed_tps_p95": float(speed_summary["p95"]),
            "speed_stability_ratio": round(float(speed_summary["p95"]) / primary_tps, 3) if primary_tps > 0 else 0.0,
            "first_token_latency_avg_ms": round(float(ttft_summary["mean"])),
            "first_token_latency_p50_ms": round(float(ttft_summary["median"])),
            "first_token_latency_p95_ms": round(float(ttft_summary["p95"])),
            "successful_rounds": self.successful_rounds,
            "failed_rounds": self.failed_rounds,
            "avg_latency_ms": round(avg_success_latency * 1000),
            "successful_avg_latency_ms": round(avg_success_latency * 1000),
            "all_avg_latency_ms": round(avg_all_latency * 1000),
            "successful_p50_latency_ms": _percentile_ms(self.successful_latency_samples_ms, 0.50),
            "successful_p95_latency_ms": _percentile_ms(self.successful_latency_samples_ms, 0.95),
            "successful_p99_latency_ms": _percentile_ms(self.successful_latency_samples_ms, 0.99),
            "all_p50_latency_ms": _percentile_ms(self.all_latency_samples_ms, 0.50),
            "all_p95_latency_ms": _percentile_ms(self.all_latency_samples_ms, 0.95),
            "all_p99_latency_ms": _percentile_ms(self.all_latency_samples_ms, 0.99),
            "timed_request_count": self.timed_request_count,
            "rounds_per_minute": round(settled / elapsed * 60, 2),
            "live_output_tokens_estimate": live_output,
            "token_rate_window_seconds": RECENT_OUTPUT_RATE_WINDOW_SECONDS,
            "speed_window_seconds": SPEED_SAMPLE_WINDOW_SECONDS,
            "quality_status": quality_status,
            "quality_reasons": quality_reasons,
            "stop_reason": stop_reason,
            "quota_limit_reached": stop_reason == "quota_exhausted",
            "service_tiers": sorted(self.service_tiers),
            "settled_token_source": settled_token_source,
            "live_token_source": live_token_source,
            "tokenizer_name": tokenizer_name,
            "elapsed_seconds": round(elapsed, 3),
        }


class BenchmarkEngine:
    """Async single-stream engine for reproducible quota and speed measurements."""

    def __init__(self) -> None:
        self.running = False
        self._stop_requested = False
        self._stats = GlobalStats()
        self._emit: EventCallback | None = None
        self._tasks: list[asyncio.Task] = []
        self._config: StressConfig | None = None
        self._records: deque[dict[str, Any]] = deque(maxlen=MAX_RECORDS)
        self._next_round_id: int = 0
        self._inflight_rounds: int = 0
        # Throttle tracking
        self._last_stats_emit: float = 0.0
        self._last_status_emit: float = 0.0
        self._worker_states: dict[int, dict] = {}
        self._stats_flush_task: asyncio.Task | None = None
        # Log batching
        self._log_buffer: list[dict] = []
        self._log_flush_task: asyncio.Task | None = None
        self._control_lock = asyncio.Lock()
        self._dropped_log_count = 0
        self._total_dropped_log_count = 0
        self._dropped_record_count = 0
        self._live_input_token_total: int = 0
        self._live_output_token_estimate_total: float = 0.0
        self._live_billable_input_token_total: int = 0
        self._live_estimated_cost_total: float = 0.0
        self._recent_live_token_events: deque[tuple[float, float]] = deque()
        self._live_round_stats: dict[int, dict[str, float]] = {}
        self._run_id = secrets.token_hex(12)
        self._stop_reason = "idle"

    @property
    def is_running(self) -> bool:
        return self.running

    @property
    def config(self) -> StressConfig | None:
        return self._config

    async def start(self, config: StressConfig, emit: EventCallback) -> None:
        if self.running:
            await emit("error", {"message": "引擎已在运行中"})
            return

        self.running = True
        self._config = config
        self._emit = emit
        self._stats = GlobalStats(test_mode=config.test_mode)
        self._records = deque(maxlen=MAX_RECORDS)
        self._worker_states = {}
        self._log_buffer = []
        self._control_lock = asyncio.Lock()
        self._dropped_log_count = 0
        self._total_dropped_log_count = 0
        self._dropped_record_count = 0
        self._live_input_token_total = 0
        self._live_output_token_estimate_total = 0.0
        self._live_billable_input_token_total = 0
        self._live_estimated_cost_total = 0.0
        self._recent_live_token_events = deque()
        self._live_round_stats = {}
        self._run_id = secrets.token_hex(12)
        self._stop_reason = "running"
        self._stop_requested = False
        self._next_round_id = 0
        self._inflight_rounds = 0
        self._last_stats_emit = 0.0
        self._last_status_emit = 0.0

        await self._send("status", {"text": "正在启动...", "color": "#22c55e"})
        run_limit = (
            f"{config.warmup_rounds} 条预热 + 最多 {config.formal_max_rounds} 条正式样本"
            if config.max_rounds > 0
            else f"{config.warmup_rounds} 条预热后，直到限额或手动停止"
        )
        mode_name = get_test_mode(config.test_mode).name
        await self._send(
            "log",
            {
                "message": (
                    f"测试启动 | 模式: {mode_name} | 模型: {config.model} | "
                    f"推理: {config.reasoning_effort} | {run_limit}"
                )
            },
        )
        clients: list[AsyncOpenAI] = []
        try:
            clients = [
                AsyncOpenAI(
                    api_key=config.api_key,
                    base_url=config.base_url,
                    timeout=REQUEST_TIMEOUT_SECONDS,
                    max_retries=0,
                )
                for _ in range(1)
            ]

            # Start stats flush loop
            self._stats_flush_task = asyncio.create_task(self._stats_flush_loop())
            # Start log flush loop
            self._log_flush_task = asyncio.create_task(self._log_flush_loop())

            workers = [asyncio.create_task(self._worker_loop(0, config, clients[0]))]
            self._tasks = workers

            with suppress(asyncio.CancelledError):
                await asyncio.gather(*workers, return_exceptions=True)
        finally:
            self.running = False

            await self._cancel_task(self._stats_flush_task)
            await self._cancel_task(self._log_flush_task)
            self._stats_flush_task = None
            self._log_flush_task = None

            # Final flush
            await self._flush_all_logs_now()

            for client in clients:
                await self._close_client(client)

            if self._stop_reason == "running":
                self._stop_reason = "completed"
            snap = self._stats.snapshot(
                live_input_tokens_hint=self._current_live_input_tokens(),
                live_output_tokens_estimate_hint=self._current_live_output_tokens_estimate(),
                live_output_tps_hint=self._current_live_output_token_rate(),
                live_cost_hint=self._current_live_estimated_cost(),
                live_billable_input_tokens_hint=self._current_live_billable_input_tokens(),
                inflight_requests_hint=self._inflight_rounds,
                active_workers_hint=self._active_worker_count(),
                waiting_workers_hint=self._waiting_worker_count(),
                settled_token_source=self._settled_token_source_label(),
                live_token_source=self._live_token_source_label(),
                tokenizer_name=self._tokenizer_name(),
                stop_reason=self._stop_reason,
                pricing_available=config.pricing_available,
            )
            await self._send("stats", snap)
            await self._send("log", {"message": f"{'=' * 50}"})
            await self._send(
                "log",
                {
                    "message": f"测试结束 | 请求: {snap.get('request_count', snap['round_count'])} | "
                    f"输入: {snap['input_tokens']:,} | 输出: {snap['output_tokens']:,} | "
                    f"总计: {snap['total_tokens']:,} | 原因: {self._stop_reason}"
                },
            )
            await self._send("log", {"message": f"{'=' * 50}"})
            await self._send("finished", snap)

    async def _worker_loop(self, worker_id: int, config: StressConfig, client: AsyncOpenAI) -> None:
        messages: list[dict[str, str]] = [{"role": "system", "content": config.system_prompt}]
        local_rounds = 0

        try:
            if not self.running or self._stop_requested:
                return

            self._set_worker_state(worker_id, "active", 0)
            local_rounds = await self._worker_loop_inner(worker_id, config, client, messages, local_rounds)
        except asyncio.CancelledError:
            pass
        finally:
            self._finalize_worker_state(worker_id, local_rounds)

    async def _worker_loop_inner(
        self,
        worker_id: int,
        config: StressConfig,
        client: AsyncOpenAI,
        messages: list[dict[str, str]],
        local_rounds: int,
    ) -> int:
        consecutive_rate_limits = 0
        consecutive_network_errors = 0
        context_retries = 0
        other_retries = 0
        while self.running:
            if self._stop_requested:
                break
            self._set_worker_state(worker_id, "active", local_rounds)

            if not self.running:
                break

            global_round = await self._reserve_round(config)
            if global_round is None:
                break

            local_rounds += 1
            inflight_round = True
            request_messages, continue_prompt_added = self._prepare_request_messages(
                config,
                messages,
                local_round=local_rounds,
                global_round=global_round,
            )

            local_prompt_t = self._estimate_messages_tokens(request_messages, config.model)
            self._register_live_round(round_id=global_round, prompt_tokens=local_prompt_t, config=config)

            self._set_worker_state(worker_id, "active", local_rounds)
            self._enqueue_log(
                f"[W{worker_id}] 第 {local_rounds} 轮开始 (全局 #{global_round}) | 上下文: {len(request_messages)} 条",
                worker_id,
            )

            # Throttled global status
            issued_requests = self._stats.successful_requests + self._stats.failed_requests + self._inflight_rounds
            await self._throttled_status(f"运行中 · {issued_requests} 请求 · 单流串行", "#22c55e")

            try:
                start_time = time.perf_counter()

                content_parts: list[str] = []
                usage = None
                finish_reason = ""
                response_status = ""
                response_service_tier = ""
                char_count = 0
                output_token_count = 0
                stream_interrupted = False
                first_token_at: float | None = None
                last_token_at: float | None = None
                stream_text_chunks = 0
                first_visible_chunk_tokens = 0

                request_kwargs = self._responses_stream_kwargs(config, request_messages)
                request_kwargs["prompt_cache_key"] = prompt_cache_key(
                    config.test_mode,
                    config.model,
                    run_id=self._run_id,
                    round_number=global_round,
                )
                response = await client.responses.create(**request_kwargs)
                async for event in self._iter_stream_chunks(response):
                    if not self.running:
                        stream_interrupted = True
                        break

                    event_type = getattr(event, "type", "")
                    if event_type == "response.output_text.delta":
                        delta_text = getattr(event, "delta", "") or ""
                        if delta_text:
                            event_at = time.perf_counter()
                            if first_token_at is None:
                                first_token_at = event_at
                                first_visible_chunk_tokens = self._estimate_text_tokens(delta_text, config.model)
                            last_token_at = event_at
                            stream_text_chunks += 1
                            content_parts.append(delta_text)
                            char_count += len(delta_text)
                            output_token_count = self._update_live_round_output_delta(
                                global_round,
                                delta_text,
                                config,
                            )
                        continue

                    if event_type == "response.output_text.done" and not content_parts:
                        done_text = getattr(event, "text", "") or ""
                        if done_text:
                            event_at = time.perf_counter()
                            first_token_at = first_token_at or event_at
                            last_token_at = last_token_at or event_at
                            stream_text_chunks = max(stream_text_chunks, 1)
                            content_parts.append(done_text)
                            char_count += len(done_text)
                            output_token_count = self._estimate_text_tokens(done_text, config.model)
                            first_visible_chunk_tokens = output_token_count
                            self._update_live_round_output_tokens(global_round, output_token_count)
                        continue

                    if event_type in {"response.completed", "response.incomplete", "response.failed"}:
                        response_obj = getattr(event, "response", None)
                        if response_obj is not None:
                            usage = getattr(response_obj, "usage", usage)
                            response_status = str(getattr(response_obj, "status", "") or "")
                            response_service_tier = str(getattr(response_obj, "service_tier", "") or "")
                            if event_type == "response.incomplete":
                                incomplete = getattr(response_obj, "incomplete_details", None)
                                reason = getattr(incomplete, "reason", None)
                                finish_reason = f"incomplete:{reason}" if reason else "incomplete"
                            elif event_type == "response.failed":
                                error_obj = getattr(response_obj, "error", None)
                                message = getattr(error_obj, "message", None) or "response failed"
                                raise RuntimeError(message)
                            else:
                                finish_reason = response_status or "completed"
                            if not content_parts:
                                completed_text = self._extract_response_output_text(response_obj)
                                if completed_text:
                                    event_at = time.perf_counter()
                                    first_token_at = first_token_at or event_at
                                    last_token_at = last_token_at or event_at
                                    stream_text_chunks = max(stream_text_chunks, 1)
                                    content_parts.append(completed_text)
                                    char_count += len(completed_text)
                                    output_token_count = self._estimate_text_tokens(completed_text, config.model)
                                    first_visible_chunk_tokens = output_token_count
                                    self._update_live_round_output_tokens(global_round, output_token_count)
                        continue

                    if event_type in {"error", "response.error"}:
                        raise RuntimeError(getattr(event, "message", None) or "Responses stream error")

                completed_at = time.perf_counter()
                latency = completed_at - start_time

                if stream_interrupted:
                    content = "".join(content_parts)
                    self._rollback_continue_prompt(messages, continue_prompt_added)
                    interrupted_output_tokens = (
                        self._estimate_text_tokens(content, config.model) if content else output_token_count
                    )
                    if inflight_round:
                        prompt_t, compl_t, total_t, estimated_cost = await self._record_interrupted_request(
                            local_prompt_t,
                            interrupted_output_tokens,
                            config,
                            output_chars=char_count,
                            latency=latency,
                        )
                        inflight_round = False
                    else:
                        prompt_t = max(int(local_prompt_t), 0)
                        compl_t = max(int(interrupted_output_tokens), 0)
                        total_t = prompt_t + compl_t
                        estimated_cost = self._estimate_round_cost(prompt_t, compl_t, 0, 0, config)
                    self._clear_live_round(global_round)
                    self._append_record(
                        {
                            "worker_id": worker_id,
                            "round_index": global_round,
                            "local_round": local_rounds,
                            "prompt_tokens": prompt_t,
                            "completion_tokens": compl_t,
                            "total_tokens": total_t,
                            "cached_tokens": 0,
                            "reasoning_tokens": 0,
                            "billable_input_tokens": prompt_t,
                            "estimated_cost": round(estimated_cost, 6),
                            "latency_seconds": latency,
                            "finish_reason": "stopped",
                            "preview": self._preview(content),
                            "timestamp": time.time(),
                        }
                    )
                    await self._throttled_stats()
                    self._enqueue_log(
                        f"[W{worker_id}] 本轮在停止过程中被中断；部分内容仅作估算，不计入精确总额 | "
                        f"入: {prompt_t:,} 出: {compl_t:,} 总: {total_t:,}",
                        worker_id,
                    )
                    break

                if usage is None:
                    raise RuntimeError("Responses API 未返回 usage，已停止本轮以避免 Token 统计不准确")

                content = "".join(content_parts)
                prompt_usage_tokens = self._usage_value(usage, "input_tokens")
                completion_usage_tokens = self._usage_value(usage, "output_tokens")
                total_usage_tokens = self._usage_value(usage, "total_tokens")
                if prompt_usage_tokens is None or completion_usage_tokens is None or total_usage_tokens is None:
                    raise RuntimeError("Responses API usage 字段不完整；本轮不计入精确结果")
                prompt_t = int(prompt_usage_tokens)
                compl_t = int(completion_usage_tokens)
                total_t = int(total_usage_tokens)
                if prompt_t < 0 or compl_t < 0 or total_t < 0:
                    raise RuntimeError("Responses API usage 含负数；本轮不计入精确结果")
                if total_t != prompt_t + compl_t:
                    raise RuntimeError("Responses API total_tokens 与 input_tokens + output_tokens 不一致")
                cached_value = self._usage_detail_value_optional(
                    usage,
                    "input_tokens_details",
                    "cached_tokens",
                )
                cache_detail_available = cached_value is not None
                cached_t = int(cached_value or 0)
                if cached_t < 0 or cached_t > prompt_t:
                    raise RuntimeError("Responses API cached_tokens 超出 input_tokens 范围")
                cache_write_value = self._usage_detail_value_optional(
                    usage,
                    "input_tokens_details",
                    "cache_write_tokens",
                )
                cache_write_detail_available = cache_write_value is not None or not config.cache_write_details_required
                cache_write_t = int(cache_write_value or 0)
                if cache_write_t < 0 or cache_write_t > prompt_t - cached_t:
                    raise RuntimeError("Responses API cache_write_tokens 超出未缓存输入范围")
                reasoning_value = self._usage_detail_value_optional(
                    usage,
                    "output_tokens_details",
                    "reasoning_tokens",
                )
                if reasoning_value is None and config.reasoning_effort != "none":
                    raise RuntimeError("Responses API 未返回 reasoning_tokens；无法准确计算可见输出速度")
                reasoning_t = int(reasoning_value or 0)
                if reasoning_t < 0 or reasoning_t > compl_t:
                    raise RuntimeError("Responses API reasoning_tokens 超出 output_tokens 范围")
                billable_input_t = max(prompt_t - cached_t, 0)
                regular_input_t = max(prompt_t - cached_t - cache_write_t, 0)
                estimated_cost = self._estimate_round_cost(
                    regular_input_t,
                    compl_t,
                    cached_t,
                    cache_write_t,
                    config,
                )
                speed = calculate_speed_measurement(
                    output_tokens=compl_t,
                    reasoning_tokens=reasoning_t,
                    request_started_at=start_time,
                    first_visible_at=first_token_at,
                    last_visible_at=last_token_at,
                    request_completed_at=completed_at,
                    stream_text_chunks=stream_text_chunks,
                    first_visible_chunk_tokens=first_visible_chunk_tokens,
                )
                is_warmup = local_rounds <= config.warmup_rounds
                actual_service_tier = response_service_tier or config.service_tier
                round_pricing_available = bool(
                    config.pricing_available and actual_service_tier == "default" and cache_write_detail_available
                )

                await self._record_success(
                    prompt_t,
                    compl_t,
                    total_t,
                    latency,
                    cached_t,
                    cache_write_t,
                    reasoning_t,
                    estimated_cost if round_pricing_available else 0.0,
                    output_chars=char_count,
                    speed=speed,
                    is_warmup=is_warmup,
                    cache_detail_available=cache_detail_available,
                    cache_write_detail_available=cache_write_detail_available,
                    service_tier=actual_service_tier,
                )
                inflight_round = False
                self._clear_live_round(global_round)

                record = {
                    "worker_id": worker_id,
                    "round_index": global_round,
                    "local_round": local_rounds,
                    "prompt_tokens": prompt_t,
                    "completion_tokens": compl_t,
                    "total_tokens": total_t,
                    "cached_tokens": cached_t,
                    "cache_write_tokens": cache_write_t,
                    "reasoning_tokens": reasoning_t,
                    "visible_output_tokens": speed.visible_output_tokens,
                    "first_visible_chunk_tokens": speed.first_visible_chunk_tokens,
                    "timed_visible_tokens": speed.timed_visible_tokens,
                    "billable_input_tokens": billable_input_t,
                    "regular_input_tokens": regular_input_t,
                    "estimated_cost": round(estimated_cost, 6) if round_pricing_available else None,
                    "latency_seconds": latency,
                    "first_token_latency_ms": round(speed.ttft_seconds * 1000) if speed.ttft_seconds > 0 else 0,
                    "time_to_last_visible_seconds": speed.time_to_last_visible_seconds,
                    "visible_generation_seconds": speed.visible_generation_seconds,
                    "visible_tokens_per_second": speed.visible_tokens_per_second,
                    "end_to_end_visible_tokens_per_second": speed.end_to_end_visible_tokens_per_second,
                    "billed_output_tokens_per_second": speed.billed_output_tokens_per_second,
                    "stream_text_chunks": speed.stream_text_chunks,
                    "speed_valid": speed.speed_valid,
                    "speed_exclusion_reason": speed.speed_exclusion_reason,
                    "is_warmup": is_warmup,
                    "service_tier": actual_service_tier,
                    "finish_reason": finish_reason,
                    "preview": self._preview(content),
                    "timestamp": time.time(),
                }
                self._append_record(record)
                await self._send("sample", record)
                consecutive_rate_limits = 0
                consecutive_network_errors = 0
                context_retries = 0
                other_retries = 0

                await self._throttled_stats()

                latency_ms = int(latency * 1000)
                first_token_ms = round(speed.ttft_seconds * 1000) if speed.ttft_seconds > 0 else 0
                cost_label = f"${estimated_cost:.6f}" if round_pricing_available else "N/A"
                self._enqueue_log(
                    f"[W{worker_id}] 第 {local_rounds} 轮完成 | "
                    f"入: {prompt_t:,} 出: {compl_t:,} 总: {total_t:,} | "
                    f"缓存读: {cached_t:,} 写: {cache_write_t:,} 思考: {reasoning_t:,} | "
                    f"首字: {first_token_ms}ms 可见速度: {speed.visible_tokens_per_second:.1f} tok/s | "
                    f"API 等效成本: {cost_label} | {latency_ms}ms | {finish_reason}",
                    worker_id,
                )

                if config.test_mode == MODE_DAILY:
                    messages.append({"role": "assistant", "content": content})

            except asyncio.CancelledError:
                partial_content = "".join(content_parts) if "content_parts" in locals() else ""
                partial_prompt_t = max(int(locals().get("local_prompt_t", 0)), 0)
                partial_output_t = self._estimate_text_tokens(partial_content, config.model) if partial_content else 0
                partial_chars = max(int(locals().get("char_count", 0)), 0)
                partial_latency = time.perf_counter() - start_time if "start_time" in locals() else 0.0
                self._rollback_continue_prompt(messages, continue_prompt_added)
                if inflight_round:
                    prompt_t, compl_t, total_t, estimated_cost = await self._record_interrupted_request(
                        partial_prompt_t,
                        partial_output_t,
                        config,
                        output_chars=partial_chars,
                        latency=partial_latency,
                    )
                    self._append_record(
                        {
                            "worker_id": worker_id,
                            "round_index": global_round,
                            "local_round": local_rounds,
                            "prompt_tokens": prompt_t,
                            "completion_tokens": compl_t,
                            "total_tokens": total_t,
                            "cached_tokens": 0,
                            "reasoning_tokens": 0,
                            "billable_input_tokens": prompt_t,
                            "estimated_cost": round(estimated_cost, 6),
                            "latency_seconds": partial_latency,
                            "finish_reason": "stopped",
                            "preview": self._preview(partial_content),
                            "timestamp": time.time(),
                        }
                    )
                    self._enqueue_log(
                        f"[W{worker_id}] 本轮被强制停止；部分内容仅作估算，不计入精确总额 | "
                        f"入: {prompt_t:,} 出: {compl_t:,} 总: {total_t:,}",
                        worker_id,
                    )
                    inflight_round = False
                self._clear_live_round(global_round)
                await self._throttled_stats()
                raise  # Let cancellation propagate
            except Exception as e:
                sensitive_values = (self.config.api_key, self.config.base_url) if self.config is not None else ()
                error_detail, failure_reason, retryable = self._classify_exception(
                    e,
                    sensitive_values=sensitive_values,
                )
                self._clear_live_round(global_round)
                self._rollback_continue_prompt(messages, continue_prompt_added)
                if inflight_round:
                    inflight_round = False
                    failure_latency = time.perf_counter() - start_time if "start_time" in locals() else 0.0
                    await self._record_request_failure(reason=failure_reason, latency=failure_latency)
                await self._throttled_stats()

                local_rounds -= 1
                err_short = (error_detail or e.__class__.__name__)[:180]
                if not retryable:
                    reason_map = {
                        "quota_exhausted": "quota_exhausted",
                        "auth": "auth_error",
                        "invalid_request": "invalid_request",
                    }
                    await self._stop_due_to_error(reason_map.get(failure_reason, "fatal_error"), err_short)
                    self._set_worker_state(worker_id, "error", local_rounds)
                    break

                if failure_reason == "context":
                    context_retries += 1
                    consecutive_rate_limits = 0
                    consecutive_network_errors = 0
                    if messages:
                        messages[:] = [messages[0]]
                    if context_retries > MAX_CONTEXT_RETRIES:
                        await self._stop_due_to_error("context_error", "连续上下文超限，已停止以避免改变测量口径")
                        self._set_worker_state(worker_id, "error", local_rounds)
                        break
                    self._enqueue_log(
                        f"[W{worker_id}] ⚠ 上下文超限，已重置；重试 {context_retries}/{MAX_CONTEXT_RETRIES}",
                        worker_id,
                    )
                elif failure_reason == "rate_limit":
                    consecutive_rate_limits += 1
                    consecutive_network_errors = 0
                    if consecutive_rate_limits > MAX_TRANSIENT_RETRIES:
                        await self._stop_due_to_error("rate_limited", "连续遇到瞬时速率限制，未确认套餐额度耗尽")
                        self._set_worker_state(worker_id, "error", local_rounds)
                        break
                    retry_delay = float(config.retry_seconds) * (2 ** (consecutive_rate_limits - 1))
                    self._enqueue_log(
                        f"[W{worker_id}] ⚠ 瞬时速率限制，{retry_delay:.2f}s 后重试 "
                        f"({consecutive_rate_limits}/{MAX_TRANSIENT_RETRIES})",
                        worker_id,
                    )
                    self._set_worker_state(worker_id, "waiting", local_rounds)
                    await asyncio.sleep(retry_delay)
                elif failure_reason == "network":
                    consecutive_rate_limits = 0
                    consecutive_network_errors += 1
                    if consecutive_network_errors > MAX_TRANSIENT_RETRIES:
                        await self._stop_due_to_error("network_error", "连续网络错误，测试未完成")
                        self._set_worker_state(worker_id, "error", local_rounds)
                        break
                    retry_delay = float(config.retry_seconds) * (2 ** (consecutive_network_errors - 1))
                    self._enqueue_log(
                        f"[W{worker_id}] ⚠ 网络错误，{retry_delay:.2f}s 后重试 "
                        f"({consecutive_network_errors}/{MAX_TRANSIENT_RETRIES})",
                        worker_id,
                    )
                    self._set_worker_state(worker_id, "waiting", local_rounds)
                    await asyncio.sleep(retry_delay)
                else:
                    consecutive_rate_limits = 0
                    consecutive_network_errors = 0
                    other_retries += 1
                    if other_retries > MAX_OTHER_RETRIES:
                        await self._stop_due_to_error("fatal_error", err_short)
                        self._set_worker_state(worker_id, "error", local_rounds)
                        break
                    retry_delay = float(config.retry_seconds)
                    self._enqueue_log(f"[W{worker_id}] ✖ 请求失败: {err_short}，仅重试一次", worker_id)
                    self._set_worker_state(worker_id, "error", local_rounds)
                    await asyncio.sleep(retry_delay)
                continue

            if config.request_interval_seconds > 0:
                self._set_worker_state(worker_id, "waiting", local_rounds)
                await asyncio.sleep(config.request_interval_seconds)

        return local_rounds

    def stop(self) -> None:
        if self._stop_reason in {"idle", "running"}:
            self._stop_reason = "manual_stop"
        self._stop_requested = True
        self.running = False
        self._cancel_worker_tasks()
        # Cancel flush loops
        if self._stats_flush_task and not self._stats_flush_task.done():
            self._stats_flush_task.cancel()
        if self._log_flush_task and not self._log_flush_task.done():
            self._log_flush_task.cancel()

    def request_stop(self) -> None:
        if self._stop_reason in {"idle", "running"}:
            self._stop_reason = "manual_stop"
        self._stop_requested = True
        self.running = False
        self._cancel_worker_tasks()

    def _cancel_worker_tasks(self) -> None:
        # Interrupt pending API calls so the stop button finishes promptly.
        for task in self._tasks:
            if not task.done():
                task.cancel()

    def get_records(self) -> list[dict]:
        return list(self._records)

    def get_stats_snapshot(self) -> dict:
        return self._stats.snapshot(
            live_input_tokens_hint=self._current_live_input_tokens(),
            live_output_tokens_estimate_hint=self._current_live_output_tokens_estimate(),
            live_output_tps_hint=self._current_live_output_token_rate(),
            live_cost_hint=self._current_live_estimated_cost(),
            live_billable_input_tokens_hint=self._current_live_billable_input_tokens(),
            inflight_requests_hint=self._inflight_rounds,
            active_workers_hint=self._active_worker_count(),
            waiting_workers_hint=self._waiting_worker_count(),
            settled_token_source=self._settled_token_source_label(),
            live_token_source=self._live_token_source_label(),
            tokenizer_name=self._tokenizer_name(),
            stop_reason=self._stop_reason,
            pricing_available=self._config.pricing_available if self._config else False,
        )

    def get_report_meta(self) -> dict[str, int]:
        return {
            "records_kept": len(self._records),
            "records_dropped": self._dropped_record_count,
            "logs_dropped": self._total_dropped_log_count,
        }

    # ── Batched event emission ──────────────────────────────────

    def _set_worker_state(self, worker_id: int, status: str, round_num: int) -> None:
        self._worker_states[worker_id] = {"id": worker_id, "status": status, "round": round_num}

    def _finalize_worker_state(self, worker_id: int, local_rounds: int) -> None:
        current_status = (self._worker_states.get(worker_id) or {}).get("status")
        if current_status == "error":
            self._set_worker_state(worker_id, "error", local_rounds)
            return
        final_status = "done" if local_rounds > 0 else "idle"
        self._set_worker_state(worker_id, final_status, local_rounds)

    @staticmethod
    def _rollback_continue_prompt(messages: list[dict[str, str]], continue_prompt_added: bool) -> None:
        if continue_prompt_added and messages:
            messages.pop()

    def _enqueue_log(self, message: str, worker_id: int = -1) -> None:
        if len(self._log_buffer) >= MAX_LOG_BUFFER:
            self._dropped_log_count += 1
            self._total_dropped_log_count += 1
            return
        self._log_buffer.append({"message": message, "worker_id": worker_id if worker_id >= 0 else None})

    async def _stats_flush_loop(self) -> None:
        try:
            while self.running:
                await asyncio.sleep(self._stats_emit_interval())
                await self._emit_stats_snapshot()
        except asyncio.CancelledError:
            pass

    async def _log_flush_loop(self) -> None:
        """Periodically flush batched log messages to clients."""
        try:
            while self.running:
                await asyncio.sleep(LOG_BATCH_SECONDS)
                await self._flush_logs_now()
        except asyncio.CancelledError:
            pass

    async def _flush_logs_now(self) -> None:
        self._promote_dropped_log_notice()
        if not self._log_buffer:
            return
        batch = self._log_buffer[:LOG_BATCH_SIZE]
        self._log_buffer = self._log_buffer[LOG_BATCH_SIZE:]
        await self._send("log_batch", batch)

    async def _flush_all_logs_now(self) -> None:
        while self._log_buffer or self._dropped_log_count:
            await self._flush_logs_now()

    async def _send(self, event_type: str, data: Any) -> None:
        if self._emit:
            with suppress(Exception):
                await self._emit(event_type, data)

    def _prepare_request_messages(
        self,
        config: StressConfig,
        conversation: list[dict[str, str]],
        *,
        local_round: int,
        global_round: int,
    ) -> tuple[list[dict[str, str]], bool]:
        if config.test_mode == MODE_DAILY:
            if is_daily_cycle_start(local_round):
                conversation[:] = [{"role": "system", "content": config.system_prompt}]
            conversation.append({"role": "user", "content": daily_turn_prompt(local_round)})
            return list(conversation), True

        system_prompt = config.system_prompt
        if config.test_mode == MODE_UNCACHED:
            marker = cache_buster(f"{self._run_id}:{global_round}")
            system_prompt = f"{marker}\n{system_prompt}"
        return [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": config.continue_prompt},
        ], False

    @staticmethod
    def _responses_common_kwargs(config: StressConfig, messages: list[dict[str, str]]) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": config.model,
            "input": messages,
            "store": False,
            "service_tier": config.service_tier or "auto",
        }
        if config.max_output_tokens > 0:
            kwargs["max_output_tokens"] = config.max_output_tokens
        if config.reasoning_effort != "none":
            kwargs["reasoning"] = {"effort": config.reasoning_effort}
        return kwargs

    @classmethod
    def _responses_stream_kwargs(cls, config: StressConfig, messages: list[dict[str, str]]) -> dict[str, Any]:
        kwargs = cls._responses_common_kwargs(config, messages)
        kwargs["stream"] = True
        return kwargs

    async def _iter_stream_chunks(self, response: Any):
        iterator = response.__aiter__()
        while self.running and not self._stop_requested:
            try:
                yield await asyncio.wait_for(
                    iterator.__anext__(),
                    timeout=STREAM_IDLE_TIMEOUT_SECONDS,
                )
            except StopAsyncIteration:
                break
            except asyncio.TimeoutError as error:
                raise TimeoutError(f"stream idle timeout after {STREAM_IDLE_TIMEOUT_SECONDS:.0f}s") from error

    async def _emit_stats_snapshot(self) -> None:
        await self._send(
            "stats",
            self._stats.snapshot(
                live_input_tokens_hint=self._current_live_input_tokens(),
                live_output_tokens_estimate_hint=self._current_live_output_tokens_estimate(),
                live_output_tps_hint=self._current_live_output_token_rate(),
                live_cost_hint=self._current_live_estimated_cost(),
                live_billable_input_tokens_hint=self._current_live_billable_input_tokens(),
                inflight_requests_hint=self._inflight_rounds,
                active_workers_hint=self._active_worker_count(),
                waiting_workers_hint=self._waiting_worker_count(),
                settled_token_source=self._settled_token_source_label(),
                live_token_source=self._live_token_source_label(),
                tokenizer_name=self._tokenizer_name(),
                stop_reason=self._stop_reason,
                pricing_available=self._config.pricing_available if self._config else False,
            ),
        )

    async def _throttled_stats(self) -> None:
        now = time.time()
        if now - self._last_stats_emit >= self._stats_emit_interval():
            self._last_stats_emit = now
            await self._emit_stats_snapshot()

    async def _throttled_status(self, text: str, color: str) -> None:
        now = time.time()
        if now - self._last_status_emit >= 0.5:
            self._last_status_emit = now
            await self._send("status", {"text": text, "color": color})

    @classmethod
    def _extract_response_output_text(cls, response: Any) -> str:
        output_text = getattr(response, "output_text", None)
        if isinstance(output_text, str):
            return output_text
        output = getattr(response, "output", None)
        if not isinstance(output, list):
            return ""
        parts: list[str] = []
        for item in output:
            content = item.get("content") if isinstance(item, dict) else getattr(item, "content", None)
            text = cls._extract_response_content_text(content)
            if text:
                parts.append(text)
        return "".join(parts)

    @classmethod
    def _extract_response_content_text(cls, content: Any) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, dict):
                    text = item.get("text") or item.get("content") or item.get("output_text") or ""
                    if text:
                        parts.append(str(text))
                else:
                    text = (
                        getattr(item, "text", None)
                        or getattr(item, "content", None)
                        or getattr(item, "output_text", None)
                        or ""
                    )
                    if text:
                        parts.append(str(text))
            return "".join(parts)
        return ""

    @staticmethod
    def _preview(content: str) -> str:
        one_line = content.replace("\n", " ")
        if len(one_line) <= PREVIEW_CHAR_LIMIT:
            return one_line
        return f"{one_line[:PREVIEW_CHAR_LIMIT]}... (共 {len(one_line)} 字符)"

    def _append_record(self, record: dict[str, Any]) -> None:
        if len(self._records) == self._records.maxlen:
            self._dropped_record_count += 1
        self._records.append(record)

    def _promote_dropped_log_notice(self) -> None:
        if self._dropped_log_count <= 0:
            return
        notice = {
            "message": f"[系统] 日志缓冲区已满，已省略 {self._dropped_log_count} 条日志",
            "worker_id": None,
        }
        self._dropped_log_count = 0
        if len(self._log_buffer) >= MAX_LOG_BUFFER:
            self._log_buffer = self._log_buffer[-(MAX_LOG_BUFFER - 1) :]
        self._log_buffer.append(notice)

    def _current_live_input_tokens(self) -> int:
        return max(int(self._live_input_token_total), 0)

    def _current_live_output_tokens_estimate(self) -> int:
        return max(int(round(self._live_output_token_estimate_total)), 0)

    def _current_live_billable_input_tokens(self) -> int:
        return max(int(self._live_billable_input_token_total), 0)

    def _current_live_estimated_cost(self) -> float:
        return max(float(self._live_estimated_cost_total), 0.0)

    def _prune_recent_live_token_events(self, now: float) -> None:
        cutoff = now - LIVE_RATE_WINDOW_SECONDS
        while self._recent_live_token_events and self._recent_live_token_events[0][0] < cutoff:
            self._recent_live_token_events.popleft()

    def _current_live_output_token_rate(self) -> float:
        now = time.time()
        self._prune_recent_live_token_events(now)
        if not self._recent_live_token_events:
            return 0.0
        window_start = max(self._stats.started_at, now - LIVE_RATE_WINDOW_SECONDS)
        elapsed = max(now - window_start, 0.25)
        return sum(tokens for _, tokens in self._recent_live_token_events) / elapsed

    def _register_live_round(self, round_id: int, prompt_tokens: int, config: StressConfig) -> None:
        prompt_tokens = max(int(prompt_tokens), 0)
        estimated_cost = self._estimate_round_cost(prompt_tokens, 0, 0, 0, config)
        self._live_round_stats[round_id] = {
            "prompt_tokens": float(prompt_tokens),
            "output_tokens": 0.0,
            "billable_input_tokens": float(prompt_tokens),
            "estimated_cost": float(estimated_cost),
            "token_tail": "",
        }
        self._live_input_token_total += prompt_tokens
        self._live_billable_input_token_total += prompt_tokens
        self._live_estimated_cost_total += estimated_cost

    def _update_live_round_output_tokens(self, round_id: int, output_tokens: float) -> None:
        round_stats = self._live_round_stats.get(round_id)
        if round_stats is None:
            return
        now = time.time()
        output_tokens = max(float(output_tokens), 0.0)
        previous = float(round_stats.get("output_tokens", 0.0))
        delta = output_tokens - previous
        if delta <= 0:
            return
        round_stats["output_tokens"] = float(output_tokens)
        self._live_output_token_estimate_total += delta
        self._recent_live_token_events.append((now, float(delta)))
        self._prune_recent_live_token_events(now)
        if self._config is not None:
            self._live_estimated_cost_total += delta * self._config.output_price_per_million / 1_000_000.0
            round_stats["estimated_cost"] = float(round_stats.get("estimated_cost", 0.0)) + (
                delta * self._config.output_price_per_million / 1_000_000.0
            )

    def _update_live_round_output_delta(self, round_id: int, delta_text: str, config: StressConfig) -> int:
        round_stats = self._live_round_stats.get(round_id)
        if round_stats is None or not delta_text:
            return 0
        previous = float(round_stats.get("output_tokens", 0.0))
        tail = str(round_stats.get("token_tail", ""))
        delta_tokens, next_tail = self._estimate_incremental_text_tokens(tail, delta_text, config.model)
        round_stats["token_tail"] = next_tail
        updated = previous + max(float(delta_tokens), 0.0)
        self._update_live_round_output_tokens(round_id, updated)
        return max(int(round(updated)), 0)

    def _clear_live_round(self, round_id: int) -> None:
        round_stats = self._live_round_stats.pop(round_id, None)
        if round_stats is None:
            return
        self._live_input_token_total = max(
            self._live_input_token_total - int(round_stats.get("prompt_tokens", 0.0)),
            0,
        )
        self._live_output_token_estimate_total = max(
            self._live_output_token_estimate_total - float(round_stats.get("output_tokens", 0.0)),
            0.0,
        )
        self._live_billable_input_token_total = max(
            self._live_billable_input_token_total - int(round_stats.get("billable_input_tokens", 0.0)),
            0,
        )
        self._live_estimated_cost_total = max(
            self._live_estimated_cost_total - float(round_stats.get("estimated_cost", 0.0)),
            0.0,
        )

    @staticmethod
    @lru_cache(maxsize=16)
    def _encoding_name_for_model(model: str) -> str:
        model = (model or "").lower()
        if model.startswith("gpt-5") or model.startswith("gpt-4o") or "o200k" in model:
            return "o200k_base"
        if model.startswith("gpt-4") or model.startswith("gpt-3.5") or "cl100k" in model:
            return "cl100k_base"
        return "o200k_base"

    @classmethod
    @lru_cache(maxsize=16)
    def _get_encoding(cls, model: str):
        encoding_name = cls._encoding_name_for_model(model)
        return tiktoken.get_encoding(encoding_name)

    @classmethod
    def _estimate_text_tokens(cls, text: str, model: str) -> int:
        if not text:
            return 0
        return len(cls._get_encoding(model).encode(text))

    @classmethod
    def _estimate_incremental_text_tokens(cls, tail_text: str, delta_text: str, model: str) -> tuple[int, str]:
        if not delta_text:
            return 0, tail_text
        encoding = cls._get_encoding(model)
        window_before = tail_text[-LIVE_TOKEN_ESTIMATE_TAIL_CHARS:]
        window_after = window_before + delta_text
        before_tokens = len(encoding.encode(window_before)) if window_before else 0
        after_tokens = len(encoding.encode(window_after))
        delta_tokens = max(after_tokens - before_tokens, 0)
        return delta_tokens, window_after[-LIVE_TOKEN_ESTIMATE_TAIL_CHARS:]

    @staticmethod
    def _exception_text(
        error: Exception,
        *,
        sensitive_values: tuple[str, ...] | list[str] = (),
    ) -> str:
        parts: list[str] = []
        message = str(error).strip()
        if message:
            parts.append(message)
        class_name = error.__class__.__name__.strip()
        if class_name and class_name.lower() not in message.lower():
            parts.append(class_name)
        status_code = getattr(error, "status_code", None)
        if status_code is None:
            status_code = getattr(getattr(error, "response", None), "status_code", None)
        if status_code is not None:
            parts.append(f"status={status_code}")
        body = getattr(error, "body", None)
        if body:
            body_text = body if isinstance(body, str) else repr(body)
            if body_text:
                parts.append(body_text)
        text = " | ".join(part for part in parts if part).strip()
        text = re.sub(r"\bsk-[A-Za-z0-9_-]+", "<redacted-key>", text)
        text = re.sub(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]+=*", "Bearer <redacted>", text)
        text = re.sub(r"\bproj_[A-Za-z0-9]+", "<redacted-project>", text)
        text = re.sub(r"(?i)(https?://)[^/\s:@]+:[^@\s/]+@", r"\1<redacted>@", text)
        text = re.sub(
            r"(?i)([?&](?:api[_-]?key|access[_-]?token|token|key)=)[^&#\s]+",
            r"\1<redacted>",
            text,
        )
        for sensitive in sorted(
            {str(value).strip() for value in sensitive_values if str(value).strip()},
            key=len,
            reverse=True,
        ):
            if len(sensitive) >= 4:
                text = re.sub(re.escape(sensitive), "<redacted-value>", text, flags=re.IGNORECASE)
        return text

    @classmethod
    def _classify_exception(
        cls,
        error: Exception,
        *,
        sensitive_values: tuple[str, ...] | list[str] = (),
    ) -> tuple[str, str, bool]:
        error_detail = cls._exception_text(error, sensitive_values=sensitive_values)
        error_msg = error_detail.lower()
        error_class = error.__class__.__name__.lower()
        status_code = getattr(error, "status_code", None)
        if status_code is None:
            status_code = getattr(getattr(error, "response", None), "status_code", None)
        if any(keyword in error_msg for keyword in QUOTA_EXHAUSTED_KEYWORDS):
            return error_detail, "quota_exhausted", False
        if any(keyword in error_msg for keyword in INVALID_REQUEST_KEYWORDS):
            return error_detail, "invalid_request", False
        if status_code in (401, 403) or any(keyword in error_msg for keyword in AUTH_KEYWORDS):
            return error_detail, "auth", False

        is_rate_limit = (
            status_code == 429 or "ratelimit" in error_class or any(kw in error_msg for kw in RATE_LIMIT_KEYWORDS)
        )
        is_context_error = any(kw in error_msg for kw in CONTEXT_LIMIT_KEYWORDS)
        if status_code is not None:
            is_context_error = status_code in (400, 413) and is_context_error
        if is_context_error and not is_rate_limit:
            return error_detail, "context", True
        if is_rate_limit:
            return error_detail, "rate_limit", True
        if any(kw in error_msg for kw in NETWORK_KEYWORDS):
            return error_detail, "network", True
        if status_code in (400, 404, 405, 409, 413, 422):
            return error_detail, "invalid_request", False
        return error_detail, "other", True

    @classmethod
    def _estimate_messages_tokens(cls, messages: list[dict[str, str]], model: str) -> int:
        encoding = cls._get_encoding(model)
        tokens_per_message = 3
        tokens_per_name = 1
        total = 3
        for message in messages:
            total += tokens_per_message
            for key, value in message.items():
                if not value:
                    continue
                total += len(encoding.encode(str(value)))
                if key == "name":
                    total += tokens_per_name
        return total

    @staticmethod
    def _usage_value(usage: Any, field_name: str) -> int | None:
        if usage is None:
            return None
        value = usage.get(field_name) if isinstance(usage, dict) else getattr(usage, field_name, None)
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _active_worker_count(self) -> int:
        return sum(1 for worker in self._worker_states.values() if (worker.get("status") or "") == "active")

    def _waiting_worker_count(self) -> int:
        return sum(1 for worker in self._worker_states.values() if (worker.get("status") or "") == "waiting")

    def _settled_token_source_label(self) -> str:
        if self._stats.successful_requests <= 0 and self._stats.interrupted_token_requests <= 0:
            return "none"
        if self._stats.exact_token_requests > 0:
            # Completed usage is always exact. Interrupted estimates live in their
            # own fields and never change the settled totals or their provenance.
            return "exact"
        if self._stats.interrupted_token_requests > 0:
            return "interrupted_estimated"
        return "none"

    def _live_token_source_label(self) -> str:
        if self._inflight_rounds <= 0 and self._current_live_output_tokens_estimate() <= 0:
            return "none"
        return "estimated"

    def _tokenizer_name(self) -> str:
        if self._config is None:
            return ""
        try:
            return self._encoding_name_for_model(self._config.model)
        except Exception:
            return ""

    def _stats_emit_interval(self) -> float:
        return STATS_THROTTLE_SECONDS

    @staticmethod
    async def _cancel_task(task: asyncio.Task | None) -> None:
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

    @staticmethod
    async def _close_client(client: AsyncOpenAI) -> None:
        close = getattr(client, "close", None) or getattr(client, "aclose", None)
        if close is None:
            return
        try:
            result = close()
            if inspect.isawaitable(result):
                await result
        except Exception:
            pass

    async def _reserve_round(self, config: StressConfig) -> int | None:
        async with self._control_lock:
            if not self.running or self._stop_requested:
                return None
            if config.max_rounds > 0 and (self._stats.successful_requests + self._inflight_rounds) >= config.max_rounds:
                if self._stop_reason == "running":
                    self._stop_reason = "max_requests"
                return None
            self._next_round_id += 1
            round_id = self._next_round_id
            self._inflight_rounds += 1
            return round_id

    async def _record_success(
        self,
        inp: int,
        out: int,
        total: int,
        latency: float,
        cached: int = 0,
        cache_write: int = 0,
        reasoning: int = 0,
        estimated_cost: float = 0.0,
        output_chars: int = 0,
        speed: SpeedMeasurement | None = None,
        is_warmup: bool = False,
        cache_detail_available: bool = True,
        cache_write_detail_available: bool = True,
        service_tier: str = "",
    ) -> None:
        async with self._control_lock:
            self._inflight_rounds = max(self._inflight_rounds - 1, 0)
            if speed is None:
                speed = calculate_speed_measurement(
                    output_tokens=out,
                    reasoning_tokens=reasoning,
                    request_started_at=0.0,
                    first_visible_at=None,
                    last_visible_at=None,
                    request_completed_at=max(float(latency), 0.0),
                    stream_text_chunks=0,
                )
            self._stats.add_success(
                inp,
                out,
                total,
                latency,
                cached=cached,
                cache_write=cache_write,
                reasoning=reasoning,
                estimated_cost=estimated_cost,
                output_chars=output_chars,
                speed=speed,
                is_warmup=is_warmup,
                cache_detail_available=cache_detail_available,
                cache_write_detail_available=cache_write_detail_available,
                service_tier=service_tier,
            )

    async def _record_request_failure(self, reason: str = "other", latency: float = 0.0) -> None:
        async with self._control_lock:
            self._inflight_rounds = max(self._inflight_rounds - 1, 0)
            self._stats.add_failed_request(reason, latency=latency)

    async def _record_interrupted_request(
        self,
        inp: int,
        out: int,
        config: StressConfig,
        *,
        output_chars: int = 0,
        latency: float = 0.0,
    ) -> tuple[int, int, int, float]:
        inp = max(int(inp), 0)
        out = max(int(out), 0)
        total = inp + out
        estimated_cost = self._estimate_round_cost(inp, out, 0, 0, config)
        async with self._control_lock:
            self._inflight_rounds = max(self._inflight_rounds - 1, 0)
            self._stats.add_interrupted_request(
                inp,
                out,
                total,
                estimated_cost,
                output_chars=output_chars,
                latency=latency,
            )
        return inp, out, total, estimated_cost

    async def _stop_due_to_error(self, reason: str, message: str) -> None:
        if self._stop_reason not in {"idle", "running"}:
            return
        self._stop_reason = reason
        self._stop_requested = True
        self.running = False
        if reason == "quota_exhausted":
            status = "已确认额度耗尽"
            color = "#f59e0b"
        else:
            status = "测试因错误停止"
            color = "#ef4444"
        safe_message = (message or reason)[:240]
        self._enqueue_log(f"[系统] {status} | {safe_message}", -1)
        await self._send("status", {"text": status, "color": color})
        current_task = asyncio.current_task()
        for task in self._tasks:
            if task is not current_task and not task.done():
                task.cancel()

    @staticmethod
    def _usage_detail_value_optional(usage: Any, detail_attr: str, field_name: str) -> int | None:
        if usage is None:
            return None
        detail = getattr(usage, detail_attr, None)
        if detail is None and isinstance(usage, dict):
            detail = usage.get(detail_attr)
        if detail is None:
            return None
        if isinstance(detail, dict):
            if field_name not in detail:
                return None
            value = detail.get(field_name)
        else:
            if not hasattr(detail, field_name):
                return None
            value = getattr(detail, field_name, None)
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _estimate_round_cost(
        regular_input_tokens: int,
        output_tokens: int,
        cached_tokens: int,
        cache_write_tokens: int,
        config: StressConfig,
    ) -> float:
        return (
            regular_input_tokens * config.input_price_per_million
            + output_tokens * config.output_price_per_million
            + cached_tokens * config.cached_price_per_million
            + cache_write_tokens * config.cache_write_price_per_million
        ) / 1_000_000.0
