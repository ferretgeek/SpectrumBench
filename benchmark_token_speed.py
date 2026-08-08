#!/usr/bin/env python3
"""Counterbalanced Responses API speed comparison with exact usage metrics."""

from __future__ import annotations

import argparse
import asyncio
import inspect
import ipaddress
import json
import secrets
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from openai import AsyncOpenAI

from stress_tool.engine_async import BenchmarkEngine
from stress_tool.measurement import (
    MEASUREMENT_VERSION,
    bootstrap_median_ratio_ci,
    calculate_speed_measurement,
    summarize,
)
from stress_tool.prompts import (
    DEFAULT_MODE_KEY,
    MODE_DAILY,
    MODE_UNCACHED,
    TEST_MODES,
    cache_buster,
    daily_turn_prompt,
    get_test_mode,
    is_daily_cycle_start,
    prompt_cache_key,
)


@dataclass(frozen=True)
class EndpointConfig:
    label: str
    base_url: str
    api_key: str
    model: str


class BenchmarkError(RuntimeError):
    pass


def configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


def normalize_base_url(value: str) -> str:
    try:
        parts = urlsplit(str(value or "").strip())
        port = parts.port
    except ValueError as error:
        raise BenchmarkError("接口地址格式无效") from error
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise BenchmarkError("接口地址必须是完整的 http:// 或 https:// 地址")
    if parts.username is not None or parts.password is not None:
        raise BenchmarkError("接口地址不能包含用户名或密码")
    if parts.query or parts.fragment:
        raise BenchmarkError("接口地址不能包含查询参数或片段")
    hostname = (parts.hostname or "").strip().lower()
    if not hostname:
        raise BenchmarkError("接口地址缺少主机名")
    if parts.scheme == "http" and not _is_loopback_host(hostname):
        raise BenchmarkError("远程接口必须使用 HTTPS；HTTP 仅允许 localhost 或回环地址")
    if port is not None and not 1 <= port <= 65535:
        raise BenchmarkError("接口端口必须在 1–65535 之间")
    route = parts.path.rstrip("/")
    if not route.endswith("/v1"):
        route = "/v1" if route in ("", "/") else f"{route}/v1"
    return urlunsplit((parts.scheme, parts.netloc.lower(), route, "", "")).rstrip("/")


def _is_loopback_host(hostname: str) -> bool:
    if hostname in {"localhost", "localhost.localdomain"}:
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def parse_endpoint_config(path: Path) -> EndpointConfig:
    try:
        lines = [line.strip() for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]
    except OSError as error:
        raise BenchmarkError(f"无法读取配置文件 {path.name}") from error
    if len(lines) < 3:
        raise BenchmarkError(f"{path.name} 格式错误：至少需要接口地址、Key、模型三行")
    base_url, api_key, model = lines[:3]
    if not api_key:
        raise BenchmarkError(f"{path.name} 的 API Key 为空")
    if not model:
        raise BenchmarkError(f"{path.name} 的模型名称为空")
    return EndpointConfig(
        label=path.name,
        base_url=normalize_base_url(base_url),
        api_key=api_key,
        model=model,
    )


def usage_value(usage: Any, field_name: str) -> int | None:
    value = usage.get(field_name) if isinstance(usage, dict) else getattr(usage, field_name, None)
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def usage_detail_value(usage: Any, detail_attr: str, field_name: str) -> int | None:
    if usage is None:
        return None
    detail = usage.get(detail_attr) if isinstance(usage, dict) else getattr(usage, detail_attr, None)
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


def extract_response_output_text(response: Any) -> str:
    output_text = getattr(response, "output_text", None)
    if isinstance(output_text, str):
        return output_text
    output = getattr(response, "output", None)
    if not isinstance(output, list):
        return ""
    parts: list[str] = []
    for item in output:
        content = item.get("content") if isinstance(item, dict) else getattr(item, "content", None)
        if not isinstance(content, list):
            continue
        for block in content:
            text = block.get("text") if isinstance(block, dict) else getattr(block, "text", None)
            if text:
                parts.append(str(text))
    return "".join(parts)


def build_request(
    *,
    mode_key: str,
    model: str,
    reasoning: str,
    run_id: str,
    sequence: int,
    input_messages: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    mode = get_test_mode(mode_key)
    if input_messages is None:
        system_prompt = mode.system_prompt
        if mode_key == MODE_UNCACHED:
            system_prompt = f"{cache_buster(f'{run_id}:{sequence}')}\n{system_prompt}"
        input_messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": mode.turn_prompts[0]},
        ]
    kwargs: dict[str, Any] = {
        "model": model,
        "input": list(input_messages),
        "stream": True,
        "store": False,
        "service_tier": "default",
        "max_output_tokens": mode.max_output_tokens,
        "prompt_cache_key": prompt_cache_key(
            mode_key,
            model,
            run_id=run_id,
            round_number=sequence,
        ),
    }
    if reasoning != "none":
        kwargs["reasoning"] = {"effort": reasoning}
    return kwargs


async def close_client(client: AsyncOpenAI) -> None:
    close = getattr(client, "close", None) or getattr(client, "aclose", None)
    if close is None:
        return
    result = close()
    if inspect.isawaitable(result):
        await result


async def run_one(
    endpoint: EndpointConfig,
    client: AsyncOpenAI,
    *,
    mode_key: str,
    reasoning: str,
    run_id: str,
    sequence: int,
    warmup: bool,
    input_messages: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    started_at = time.perf_counter()
    first_visible_at: float | None = None
    last_visible_at: float | None = None
    stream_text_chunks = 0
    text_parts: list[str] = []
    first_visible_chunk_text = ""
    usage = None
    status = ""
    finish_reason = ""
    actual_service_tier = ""

    stream = await client.responses.create(
        **build_request(
            mode_key=mode_key,
            model=endpoint.model,
            reasoning=reasoning,
            run_id=run_id,
            sequence=sequence,
            input_messages=input_messages,
        )
    )
    async for event in stream:
        event_type = getattr(event, "type", "")
        if event_type == "response.output_text.delta":
            delta = getattr(event, "delta", "") or ""
            if delta:
                event_at = time.perf_counter()
                if first_visible_at is None:
                    first_visible_chunk_text = delta
                first_visible_at = first_visible_at or event_at
                last_visible_at = event_at
                stream_text_chunks += 1
                text_parts.append(delta)
        elif event_type == "response.output_text.done" and not text_parts:
            done_text = getattr(event, "text", "") or ""
            if done_text:
                event_at = time.perf_counter()
                first_visible_at = event_at
                last_visible_at = event_at
                stream_text_chunks = 1
                text_parts.append(done_text)
                first_visible_chunk_text = done_text
        elif event_type in {"response.completed", "response.incomplete", "response.failed"}:
            response = getattr(event, "response", None)
            if response is not None:
                usage = getattr(response, "usage", usage)
                status = str(getattr(response, "status", "") or event_type)
                actual_service_tier = str(getattr(response, "service_tier", "") or "")
                if event_type == "response.incomplete":
                    details = getattr(response, "incomplete_details", None)
                    finish_reason = f"incomplete:{getattr(details, 'reason', '') or 'unknown'}"
                elif event_type == "response.failed":
                    error = getattr(response, "error", None)
                    raise BenchmarkError(getattr(error, "message", None) or "response failed")
                else:
                    finish_reason = status or "completed"
                if not text_parts:
                    response_text = extract_response_output_text(response)
                    if response_text:
                        event_at = time.perf_counter()
                        first_visible_at = event_at
                        last_visible_at = event_at
                        stream_text_chunks = 1
                        text_parts.append(response_text)
                        first_visible_chunk_text = response_text
        elif event_type in {"error", "response.error"}:
            raise BenchmarkError(getattr(event, "message", None) or "Responses stream error")

    completed_at = time.perf_counter()
    if usage is None:
        raise BenchmarkError(f"{endpoint.label} 第 {sequence} 次没有返回 usage")
    input_tokens = usage_value(usage, "input_tokens")
    output_tokens = usage_value(usage, "output_tokens")
    total_tokens = usage_value(usage, "total_tokens")
    if input_tokens is None or output_tokens is None or total_tokens is None:
        raise BenchmarkError(f"{endpoint.label} 第 {sequence} 次 usage 字段不完整")

    input_tokens = int(input_tokens)
    output_tokens = int(output_tokens)
    total_tokens = int(total_tokens)
    if input_tokens < 0 or output_tokens < 0 or total_tokens < 0:
        raise BenchmarkError(f"{endpoint.label} 第 {sequence} 次 usage 含负数")
    if total_tokens != input_tokens + output_tokens:
        raise BenchmarkError(f"{endpoint.label} 第 {sequence} 次 total_tokens 与 input_tokens + output_tokens 不一致")

    cached_value = usage_detail_value(usage, "input_tokens_details", "cached_tokens")
    if cached_value is None:
        raise BenchmarkError(f"{endpoint.label} 第 {sequence} 次缺少 cached_tokens，无法验证模式")
    reasoning_value = usage_detail_value(usage, "output_tokens_details", "reasoning_tokens")
    if reasoning != "none" and reasoning_value is None:
        raise BenchmarkError(f"{endpoint.label} 第 {sequence} 次缺少 reasoning_tokens，无法计算可见速度")
    cached_tokens = int(cached_value)
    if cached_tokens < 0 or cached_tokens > input_tokens:
        raise BenchmarkError(f"{endpoint.label} 第 {sequence} 次 cached_tokens 超出输入范围")
    cache_write_value = usage_detail_value(usage, "input_tokens_details", "cache_write_tokens")
    cache_write_tokens = int(cache_write_value) if cache_write_value is not None else None
    if cache_write_tokens is not None and (cache_write_tokens < 0 or cache_write_tokens > input_tokens - cached_tokens):
        raise BenchmarkError(f"{endpoint.label} 第 {sequence} 次 cache_write_tokens 超出未缓存输入范围")
    reasoning_tokens = int(reasoning_value or 0)
    if reasoning_tokens < 0 or reasoning_tokens > output_tokens:
        raise BenchmarkError(f"{endpoint.label} 第 {sequence} 次 reasoning_tokens 超出输出范围")
    first_visible_chunk_tokens = (
        BenchmarkEngine._estimate_text_tokens(
            first_visible_chunk_text,
            endpoint.model,
        )
        if first_visible_chunk_text
        else 0
    )
    speed = calculate_speed_measurement(
        output_tokens=output_tokens,
        reasoning_tokens=reasoning_tokens,
        request_started_at=started_at,
        first_visible_at=first_visible_at,
        last_visible_at=last_visible_at,
        request_completed_at=completed_at,
        stream_text_chunks=stream_text_chunks,
        first_visible_chunk_tokens=first_visible_chunk_tokens,
    )
    if not warmup and not speed.speed_valid:
        raise BenchmarkError(f"{endpoint.label} 第 {sequence} 次不能形成有效速度样本: {speed.speed_exclusion_reason}")

    text = "".join(text_parts)
    return {
        "sequence": sequence,
        "endpoint": endpoint.label,
        "model": endpoint.model,
        "mode": mode_key,
        "reasoning": reasoning,
        "warmup": bool(warmup),
        "status": status,
        "finish_reason": finish_reason,
        "service_tier": actual_service_tier or "default",
        "char_count": len(text),
        "input_tokens": input_tokens,
        "cached_tokens": cached_tokens,
        "cache_write_tokens": cache_write_tokens,
        "cache_read_ratio": round(cached_tokens / input_tokens, 4) if input_tokens > 0 else 0.0,
        "output_tokens": output_tokens,
        "reasoning_tokens": reasoning_tokens,
        "visible_output_tokens": speed.visible_output_tokens,
        "first_visible_chunk_tokens": speed.first_visible_chunk_tokens,
        "timed_visible_tokens": speed.timed_visible_tokens,
        "total_tokens": max(total_tokens, input_tokens + output_tokens),
        "ttft_ms": round(speed.ttft_seconds * 1000, 2),
        "time_to_last_visible_ms": round(speed.time_to_last_visible_seconds * 1000, 2),
        "request_latency_ms": round(speed.request_seconds * 1000, 2),
        "visible_generation_seconds": speed.visible_generation_seconds,
        "visible_tokens_per_second": speed.visible_tokens_per_second,
        "end_to_end_visible_tokens_per_second": speed.end_to_end_visible_tokens_per_second,
        "billed_output_tokens_per_second": speed.billed_output_tokens_per_second,
        "stream_text_chunks": speed.stream_text_chunks,
        "speed_valid": speed.speed_valid,
        "speed_exclusion_reason": speed.speed_exclusion_reason,
        "_response_text": "".join(text_parts),
    }


async def run_one_with_retries(
    endpoint: EndpointConfig,
    client: AsyncOpenAI,
    *,
    mode_key: str,
    reasoning: str,
    run_id: str,
    sequence: int,
    warmup: bool,
    input_messages: list[dict[str, str]] | None,
    retries: int,
    retry_delay: float,
) -> dict[str, Any]:
    last_error: Exception | None = None
    for attempt in range(1, max(int(retries), 0) + 2):
        try:
            return await run_one(
                endpoint,
                client,
                mode_key=mode_key,
                reasoning=reasoning,
                run_id=run_id,
                sequence=sequence,
                warmup=warmup,
                input_messages=input_messages,
            )
        except Exception as error:
            last_error = error
            detail, category, retryable = BenchmarkEngine._classify_exception(
                error,
                sensitive_values=(endpoint.api_key, endpoint.base_url),
            )
            if not retryable or attempt > retries:
                break
            delay = max(float(retry_delay), 0.0) * (2 ** (attempt - 1))
            print(
                json.dumps(
                    {
                        "phase": "retry",
                        "endpoint": endpoint.label,
                        "sample": sequence,
                        "attempt": attempt,
                        "category": category,
                        "delay_seconds": delay,
                        "message": detail[:180],
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            await asyncio.sleep(delay)
    assert last_error is not None
    safe_detail = BenchmarkEngine._exception_text(
        last_error,
        sensitive_values=(endpoint.api_key, endpoint.base_url),
    )
    raise BenchmarkError(safe_detail or last_error.__class__.__name__) from None


def balanced_order(endpoints: list[EndpointConfig], sample_index: int) -> list[EndpointConfig]:
    if len(endpoints) <= 1:
        return list(endpoints)
    if len(endpoints) == 2:
        return list(endpoints) if sample_index % 2 == 1 else list(reversed(endpoints))
    offset = (sample_index - 1) % len(endpoints)
    rotated = endpoints[offset:] + endpoints[:offset]
    block = (sample_index - 1) // len(endpoints)
    return list(reversed(rotated)) if block % 2 else rotated


def prepare_daily_messages(
    conversation: list[dict[str, str]],
    scenario_round: int,
) -> list[dict[str, str]]:
    mode = get_test_mode(MODE_DAILY)
    if is_daily_cycle_start(scenario_round):
        conversation[:] = [{"role": "system", "content": mode.system_prompt}]
    conversation.append({"role": "user", "content": daily_turn_prompt(scenario_round)})
    return list(conversation)


def build_summary(results: list[dict[str, Any]], mode_key: str) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in results:
        grouped.setdefault(str(item["endpoint"]), []).append(item)
    mode = get_test_mode(mode_key)
    summary: dict[str, Any] = {}
    for endpoint, rows in grouped.items():
        total_input = sum(int(row["input_tokens"]) for row in rows)
        total_cached = sum(int(row["cached_tokens"]) for row in rows)
        cache_ratio = total_cached / total_input if total_input > 0 else 0.0
        cache_valid: bool | None
        if mode.cache_policy == "none":
            cache_valid = cache_ratio <= mode.cache_ratio_max
        elif mode.cache_policy == "high":
            cache_valid = cache_ratio >= mode.cache_ratio_min
        else:
            cache_valid = None
        summary[endpoint] = {
            "samples": len(rows),
            "model": rows[0]["model"],
            "mode": mode_key,
            "reasoning": rows[0]["reasoning"],
            "visible_tokens_per_second": summarize(
                (row["visible_tokens_per_second"] for row in rows),
                digits=2,
            ),
            "end_to_end_visible_tokens_per_second": summarize(
                (row["end_to_end_visible_tokens_per_second"] for row in rows),
                digits=2,
            ),
            "billed_output_tokens_per_second": summarize(
                (row["billed_output_tokens_per_second"] for row in rows),
                digits=2,
            ),
            "ttft_ms": summarize((row["ttft_ms"] for row in rows), digits=1),
            "request_latency_ms": summarize((row["request_latency_ms"] for row in rows), digits=1),
            "visible_output_tokens": summarize((row["visible_output_tokens"] for row in rows), digits=1),
            "cache_read_ratio": round(cache_ratio, 4),
            "cache_validation_passed": cache_valid,
            "service_tiers": sorted({str(row["service_tier"]) for row in rows}),
            "total_input_tokens": total_input,
            "total_cached_tokens": total_cached,
            "total_cache_write_tokens": (
                None
                if any(row["cache_write_tokens"] is None for row in rows)
                else sum(int(row["cache_write_tokens"]) for row in rows)
            ),
            "total_output_tokens": sum(int(row["output_tokens"]) for row in rows),
            "total_reasoning_tokens": sum(int(row["reasoning_tokens"]) for row in rows),
            "total_visible_output_tokens": sum(int(row["visible_output_tokens"]) for row in rows),
        }
    return summary


def build_comparisons(
    results: list[dict[str, Any]],
    endpoints: list[EndpointConfig],
) -> dict[str, Any]:
    if not endpoints:
        return {}
    baseline = endpoints[0]
    by_endpoint_sequence = {(str(row["endpoint"]), int(row["sequence"])): row for row in results}
    comparisons: dict[str, Any] = {}
    for endpoint in endpoints:
        if endpoint.label == baseline.label:
            continue
        baseline_visible: list[float] = []
        candidate_visible: list[float] = []
        baseline_e2e: list[float] = []
        candidate_e2e: list[float] = []
        for sequence in sorted({int(row["sequence"]) for row in results}):
            left = by_endpoint_sequence.get((baseline.label, sequence))
            right = by_endpoint_sequence.get((endpoint.label, sequence))
            if left is None or right is None:
                continue
            baseline_visible.append(float(left["visible_tokens_per_second"]))
            candidate_visible.append(float(right["visible_tokens_per_second"]))
            baseline_e2e.append(float(left["end_to_end_visible_tokens_per_second"]))
            candidate_e2e.append(float(right["end_to_end_visible_tokens_per_second"]))
        reasons: list[str] = []
        if endpoint.model != baseline.model:
            reasons.append("模型不同")
        baseline_rows = [row for row in results if row["endpoint"] == baseline.label]
        candidate_rows = [row for row in results if row["endpoint"] == endpoint.label]
        if any(str(row.get("service_tier")) != "default" for row in baseline_rows + candidate_rows):
            reasons.append("实际服务层级不是 default")
        mode = get_test_mode(str(results[0]["mode"])) if results else None
        if mode is not None and mode.cache_policy in {"none", "high"}:
            for label, rows in ((baseline.label, baseline_rows), (endpoint.label, candidate_rows)):
                total_input = sum(int(row["input_tokens"]) for row in rows)
                total_cached = sum(int(row["cached_tokens"]) for row in rows)
                ratio = total_cached / total_input if total_input > 0 else 0.0
                valid = ratio <= mode.cache_ratio_max if mode.cache_policy == "none" else ratio >= mode.cache_ratio_min
                if not valid:
                    reasons.append(f"{label} 缓存口径未达标")
        if len(baseline_visible) < 3:
            reasons.append("有效配对样本少于 3 组")
        for label, values in (
            (baseline.label, baseline_visible),
            (endpoint.label, candidate_visible),
        ):
            speed_stats = summarize(values, digits=4)
            if int(speed_stats["count"]) >= 3 and float(speed_stats["cv"]) > 0.15:
                reasons.append(f"{label} 可见速度 CV 高于 15%")
        comparable = not reasons
        comparisons[endpoint.label] = {
            "baseline": baseline.label,
            "candidate": endpoint.label,
            "comparable": comparable,
            "comparability_notes": reasons,
            "paired_samples": len(baseline_visible),
            "visible_speed_ratio": (
                bootstrap_median_ratio_ci(candidate_visible, baseline_visible) if comparable else None
            ),
            "end_to_end_ratio": (bootstrap_median_ratio_ci(candidate_e2e, baseline_e2e) if comparable else None),
        }
    return comparisons


async def run_benchmark(args: argparse.Namespace) -> dict[str, Any]:
    config_paths = args.configs or []
    if not config_paths:
        raise BenchmarkError("请用 --configs 显式指定本地三行配置文件；程序不会自动发现凭据文件")
    endpoints = [parse_endpoint_config(Path(path)) for path in config_paths]
    labels = [endpoint.label for endpoint in endpoints]
    if len(set(labels)) != len(labels):
        raise BenchmarkError("配置文件名必须唯一，避免报告分组冲突")

    mode = get_test_mode(args.mode)
    reasoning = args.reasoning or mode.default_reasoning
    if reasoning not in mode.allowed_reasoning:
        raise BenchmarkError(f"{mode.name} 只允许推理等级: {', '.join(mode.allowed_reasoning)}")
    run_id = secrets.token_hex(12)
    clients = {
        endpoint.label: AsyncOpenAI(
            api_key=endpoint.api_key,
            base_url=endpoint.base_url,
            timeout=args.timeout,
            max_retries=0,
        )
        for endpoint in endpoints
    }
    formal_results: list[dict[str, Any]] = []
    warmup_results: list[dict[str, Any]] = []
    conversations = {endpoint.label: [{"role": "system", "content": mode.system_prompt}] for endpoint in endpoints}
    started_at = datetime.now().isoformat(timespec="seconds")

    def prepare_input(endpoint: EndpointConfig, scenario_round: int) -> list[dict[str, str]] | None:
        if args.mode != MODE_DAILY:
            return None
        return prepare_daily_messages(conversations[endpoint.label], scenario_round)

    def settle_result(endpoint: EndpointConfig, result: dict[str, Any]) -> dict[str, Any]:
        response_text = str(result.pop("_response_text", ""))
        if args.mode == MODE_DAILY:
            conversations[endpoint.label].append({"role": "assistant", "content": response_text})
        return result

    try:
        for warmup_index in range(1, args.warmups + 1):
            for endpoint in endpoints:
                print(
                    json.dumps(
                        {
                            "phase": "warmup_start",
                            "warmup": warmup_index,
                            "endpoint": endpoint.label,
                            "model": endpoint.model,
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
                result = await run_one_with_retries(
                    endpoint,
                    clients[endpoint.label],
                    mode_key=args.mode,
                    reasoning=reasoning,
                    run_id=run_id,
                    sequence=-warmup_index,
                    warmup=True,
                    input_messages=prepare_input(endpoint, warmup_index),
                    retries=args.retries,
                    retry_delay=args.retry_delay,
                )
                result = settle_result(endpoint, result)
                warmup_results.append(result)
                print(json.dumps({"phase": "warmup_ok", **result}, ensure_ascii=False), flush=True)

        for sample_index in range(1, args.samples + 1):
            for endpoint in balanced_order(endpoints, sample_index):
                print(
                    json.dumps(
                        {
                            "phase": "sample_start",
                            "sample": sample_index,
                            "endpoint": endpoint.label,
                            "model": endpoint.model,
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
                result = await run_one_with_retries(
                    endpoint,
                    clients[endpoint.label],
                    mode_key=args.mode,
                    reasoning=reasoning,
                    run_id=run_id,
                    sequence=sample_index,
                    warmup=False,
                    input_messages=prepare_input(endpoint, args.warmups + sample_index),
                    retries=args.retries,
                    retry_delay=args.retry_delay,
                )
                result = settle_result(endpoint, result)
                formal_results.append(result)
                print(json.dumps({"phase": "sample_ok", **result}, ensure_ascii=False), flush=True)
    finally:
        await asyncio.gather(*(close_client(client) for client in clients.values()), return_exceptions=True)

    report = {
        "schema_version": 3,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "started_at": started_at,
        "method": {
            "configs": [Path(path).name for path in config_paths],
            "samples_per_endpoint": args.samples,
            "warmups_per_endpoint": args.warmups,
            "mode": args.mode,
            "mode_name": mode.name,
            "mode_version": mode.version,
            "measurement_version": MEASUREMENT_VERSION,
            "reasoning": reasoning,
            "stream": True,
            "store": False,
            "service_tier": "default",
            "connection_reuse": True,
            "order": "两端点奇偶轮反转；三端点及以上轮转并按区块反转",
            "primary_speed_definition": "(visible_output_tokens - estimated_first_visible_chunk_tokens) / (last_visible_event - first_visible_event)",
            "visible_output_tokens_definition": "usage.output_tokens - usage.output_tokens_details.reasoning_tokens",
            "end_to_end_definition": "visible_output_tokens / (last_visible_event - request_start)",
            "warmup_policy": "预热不进入正式速度统计；报告单独保留预热 usage 与缓存结果",
            "daily_context_policy": "日常模式按六轮增长上下文；每个端点维护自己的会话，预热轮也进入上下文",
        },
        "summary": build_summary(formal_results, args.mode),
        "comparisons": build_comparisons(formal_results, endpoints),
        "warmups": warmup_results,
        "samples": formal_results,
    }
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="使用统一、可审计口径对比 Responses API token 速度")
    parser.add_argument("--configs", nargs="+", help="三行格式：接口地址、API Key、模型；可传一个或多个文件")
    parser.add_argument("--mode", choices=tuple(TEST_MODES), default=DEFAULT_MODE_KEY)
    parser.add_argument("--reasoning", choices=("none", "low", "medium", "high", "xhigh"))
    parser.add_argument("--samples", type=int, default=6)
    parser.add_argument("--warmups", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=240.0)
    parser.add_argument("--retries", type=int, default=1)
    parser.add_argument("--retry-delay", type=float, default=2.0)
    parser.add_argument(
        "--output",
        default=f"benchmark_results/token_speed_compare_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json",
    )
    return parser


def main() -> None:
    configure_stdio()
    parser = build_parser()
    args = parser.parse_args()
    if args.samples < 1:
        raise BenchmarkError("samples 必须大于 0")
    if args.warmups < 0:
        raise BenchmarkError("warmups 不能小于 0")
    if args.timeout <= 0:
        raise BenchmarkError("timeout 必须大于 0")
    if args.retries < 0:
        raise BenchmarkError("retries 不能小于 0")
    try:
        report = asyncio.run(run_benchmark(args))
    except Exception as error:
        detail = BenchmarkEngine._exception_text(error)
        print(json.dumps({"phase": "error", "message": detail[:500]}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(1) from None
    print(
        json.dumps(
            {
                "phase": "done",
                "output": args.output,
                "summary": report["summary"],
                "comparisons": report["comparisons"],
            },
            ensure_ascii=False,
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
