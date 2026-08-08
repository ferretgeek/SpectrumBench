"""Shared, deterministic measurement helpers.

The project reports three different rates on purpose:

* visible TPS: official visible output tokens received after the first text
  chunk, divided by the time between the first and last text events;
* end-to-end visible TPS: visible text tokens divided by time to the last text event;
* billed output TPS: all output tokens (including reasoning) divided by request time.

Keeping these definitions in one module prevents the dashboard and CLI benchmark
from silently drifting apart again.
"""

from __future__ import annotations

import math
import random
import statistics
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass

MIN_VISIBLE_TOKENS_FOR_SPEED = 32
MIN_VISIBLE_SECONDS_FOR_SPEED = 0.05
MEASUREMENT_VERSION = 3


def percentile(values: Sequence[float], quantile: float) -> float:
    """Return a linearly interpolated percentile for ``0 <= quantile <= 1``."""
    if not values:
        return 0.0
    ordered = sorted(float(value) for value in values)
    if len(ordered) == 1:
        return ordered[0]
    position = min(max(float(quantile), 0.0), 1.0) * (len(ordered) - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def summarize(values: Iterable[float], *, digits: int = 3) -> dict[str, float | int]:
    clean = [float(value) for value in values if math.isfinite(float(value))]
    if not clean:
        return {
            "count": 0,
            "mean": 0.0,
            "median": 0.0,
            "p25": 0.0,
            "p75": 0.0,
            "p95": 0.0,
            "min": 0.0,
            "max": 0.0,
            "stdev": 0.0,
            "cv": 0.0,
        }
    mean = statistics.fmean(clean)
    stdev = statistics.stdev(clean) if len(clean) >= 2 else 0.0
    result: dict[str, float | int] = {
        "count": len(clean),
        "mean": mean,
        "median": statistics.median(clean),
        "p25": percentile(clean, 0.25),
        "p75": percentile(clean, 0.75),
        "p95": percentile(clean, 0.95),
        "min": min(clean),
        "max": max(clean),
        "stdev": stdev,
        "cv": stdev / mean if mean > 0 else 0.0,
    }
    return {key: value if isinstance(value, int) else round(value, digits) for key, value in result.items()}


@dataclass(frozen=True)
class SpeedMeasurement:
    output_tokens: int
    reasoning_tokens: int
    visible_output_tokens: int
    first_visible_chunk_tokens: int
    timed_visible_tokens: int
    request_seconds: float
    ttft_seconds: float
    time_to_last_visible_seconds: float
    visible_generation_seconds: float
    visible_tokens_per_second: float
    end_to_end_visible_tokens_per_second: float
    billed_output_tokens_per_second: float
    stream_text_chunks: int
    speed_valid: bool
    speed_exclusion_reason: str

    def to_dict(self) -> dict[str, int | float | bool | str]:
        return asdict(self)


def calculate_speed_measurement(
    *,
    output_tokens: int,
    reasoning_tokens: int,
    request_started_at: float,
    first_visible_at: float | None,
    last_visible_at: float | None,
    request_completed_at: float,
    stream_text_chunks: int,
    first_visible_chunk_tokens: int = 1,
) -> SpeedMeasurement:
    """Calculate non-overlapping speed metrics from monotonic timestamps.

    ``output_tokens`` includes hidden reasoning tokens in Responses API usage.
    Those tokens are deliberately excluded from visible TPS because they are
    produced before visible text and have no per-token stream timestamps.
    """
    output = max(int(output_tokens), 0)
    reasoning = max(0, min(int(reasoning_tokens), output))
    visible = max(output - reasoning, 0)
    first_chunk = min(max(int(first_visible_chunk_tokens), 1), visible) if visible > 0 else 0
    timed_visible = max(visible - first_chunk, 0)
    request_seconds = max(float(request_completed_at) - float(request_started_at), 0.0)

    if first_visible_at is None or last_visible_at is None:
        ttft = 0.0
        time_to_last = 0.0
        visible_seconds = 0.0
    else:
        first = max(float(first_visible_at), float(request_started_at))
        last = max(float(last_visible_at), first)
        ttft = first - float(request_started_at)
        time_to_last = last - float(request_started_at)
        visible_seconds = last - first

    valid = True
    exclusion_reason = ""
    if visible < MIN_VISIBLE_TOKENS_FOR_SPEED:
        valid = False
        exclusion_reason = "visible_output_too_short"
    elif int(stream_text_chunks) < 2:
        valid = False
        exclusion_reason = "stream_buffered_single_chunk"
    elif visible_seconds < MIN_VISIBLE_SECONDS_FOR_SPEED:
        valid = False
        exclusion_reason = "visible_window_too_short"
    elif timed_visible <= 0:
        valid = False
        exclusion_reason = "first_visible_chunk_covers_output"

    # The whole first SSE text chunk has already arrived at t=0 of the visible
    # phase. Subtracting that chunk avoids treating buffered first-chunk tokens
    # as if they were generated during the measured interval.
    visible_tps = timed_visible / visible_seconds if valid else 0.0
    e2e_visible_tps = visible / time_to_last if time_to_last > 0 else 0.0
    billed_output_tps = output / request_seconds if request_seconds > 0 else 0.0

    return SpeedMeasurement(
        output_tokens=output,
        reasoning_tokens=reasoning,
        visible_output_tokens=visible,
        first_visible_chunk_tokens=first_chunk,
        timed_visible_tokens=timed_visible,
        request_seconds=round(request_seconds, 6),
        ttft_seconds=round(ttft, 6),
        time_to_last_visible_seconds=round(time_to_last, 6),
        visible_generation_seconds=round(visible_seconds, 6),
        visible_tokens_per_second=round(visible_tps, 3),
        end_to_end_visible_tokens_per_second=round(e2e_visible_tps, 3),
        billed_output_tokens_per_second=round(billed_output_tps, 3),
        stream_text_chunks=max(int(stream_text_chunks), 0),
        speed_valid=valid,
        speed_exclusion_reason=exclusion_reason,
    )


def bootstrap_median_ratio_ci(
    numerators: Sequence[float],
    denominators: Sequence[float],
    *,
    confidence: float = 0.95,
    resamples: int = 4000,
    seed: int = 20260714,
) -> dict[str, float | int]:
    """Paired median ratio with a deterministic percentile bootstrap CI."""
    pairs = [
        (float(numerator), float(denominator))
        for numerator, denominator in zip(numerators, denominators, strict=True)
        if math.isfinite(float(numerator))
        and math.isfinite(float(denominator))
        and float(numerator) > 0
        and float(denominator) > 0
    ]
    if not pairs:
        return {"pairs": 0, "median_ratio": 0.0, "ci_low": 0.0, "ci_high": 0.0}

    ratios = [numerator / denominator for numerator, denominator in pairs]
    estimate = statistics.median(ratios)
    if len(ratios) == 1:
        return {
            "pairs": 1,
            "median_ratio": round(estimate, 4),
            "ci_low": round(estimate, 4),
            "ci_high": round(estimate, 4),
        }

    rng = random.Random(seed)
    samples: list[float] = []
    for _ in range(max(int(resamples), 100)):
        draw = [ratios[rng.randrange(len(ratios))] for _ in ratios]
        samples.append(statistics.median(draw))
    alpha = max(0.0, min(1.0 - float(confidence), 1.0)) / 2.0
    return {
        "pairs": len(ratios),
        "median_ratio": round(estimate, 4),
        "ci_low": round(percentile(samples, alpha), 4),
        "ci_high": round(percentile(samples, 1.0 - alpha), 4),
    }
