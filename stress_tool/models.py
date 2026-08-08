"""Shared data models for the benchmark application."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from math import isfinite
from typing import Any


@dataclass(frozen=True)
class StressConfig:
    api_key: str
    base_url: str
    model: str
    test_mode: str
    reasoning_effort: str
    system_prompt: str
    continue_prompt: str
    max_rounds: int
    request_interval_seconds: float
    retry_seconds: int
    run_label: str = ""
    num_workers: int = 1
    max_output_tokens: int = 0
    warmup_rounds: int = 1
    input_price_per_million: float = 0.0
    output_price_per_million: float = 0.0
    cached_price_per_million: float = 0.0
    cache_write_price_per_million: float = 0.0
    pricing_model_id: str = ""
    pricing_updated_at: str = ""
    pricing_source: str = ""
    service_tier: str = "default"
    context_keep_recent: int = field(default=8, repr=False)

    def __post_init__(self) -> None:
        for attr, field_name in (
            ("api_key", "API Key"),
            ("base_url", "Base URL"),
            ("model", "模型名称"),
            ("test_mode", "测试模式"),
            ("service_tier", "服务层级"),
            ("system_prompt", "系统提示词"),
            ("continue_prompt", "续写提示词"),
        ):
            value = getattr(self, attr)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name}不能为空")

        for attr, minimum, field_name in (
            ("max_rounds", 0, "最大请求数"),
            ("retry_seconds", 0, "重试等待"),
            ("num_workers", 1, "并发数"),
            ("max_output_tokens", 0, "最大输出 Token"),
            ("warmup_rounds", 0, "预热请求数"),
            ("context_keep_recent", 2, "上下文保留条数"),
        ):
            if int(getattr(self, attr)) < minimum:
                raise ValueError(f"{field_name}不能小于 {minimum}")

        if int(self.num_workers) != 1:
            raise ValueError("基准测试固定为单流串行请求，并发数必须为 1")

        for attr, minimum, field_name in (
            ("request_interval_seconds", 0.0, "轮间隔"),
            ("input_price_per_million", 0.0, "输入价格"),
            ("output_price_per_million", 0.0, "输出价格"),
            ("cached_price_per_million", 0.0, "缓存读取价格"),
            ("cache_write_price_per_million", 0.0, "缓存写入价格"),
        ):
            value = float(getattr(self, attr))
            if not isfinite(value):
                raise ValueError(f"{field_name}必须是有限数字")
            if value < minimum:
                raise ValueError(f"{field_name}不能小于 {minimum}")

    @property
    def pricing_available(self) -> bool:
        return bool(self.pricing_model_id and self.output_price_per_million > 0)

    @property
    def formal_max_rounds(self) -> int:
        """User-facing formal sample limit; zero means unlimited."""
        if self.max_rounds <= 0:
            return 0
        return max(self.max_rounds - self.warmup_rounds, 0)

    @property
    def cache_write_details_required(self) -> bool:
        """Whether pricing needs a separate cache-write token count."""
        return self.pricing_available and self.cache_write_price_per_million > self.input_price_per_million + 1e-12

    def to_public_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["api_key"] = "***"
        data["pricing_available"] = self.pricing_available
        data["cache_write_details_required"] = self.cache_write_details_required
        data["formal_max_rounds"] = self.formal_max_rounds
        data.pop("context_keep_recent", None)
        data.pop("system_prompt", None)
        data.pop("continue_prompt", None)
        return data
