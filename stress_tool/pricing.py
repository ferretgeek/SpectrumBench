"""Load and resolve the checked-in API-equivalent pricing table."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent.parent
PRICING_FILE = BASE_DIR / "pricing_table.json"
PRICE_FIELDS = ("input", "cached_input", "cache_write", "output")


def load_pricing_table(path: Path = PRICING_FILE) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"定价表读取失败: {error}") from error
    if not isinstance(raw, dict) or not isinstance(raw.get("models"), dict):
        raise ValueError("定价表格式错误: models 必须是对象")
    return raw


def resolve_model_pricing(model: str, table: dict[str, Any] | None = None) -> dict[str, Any] | None:
    table = load_pricing_table() if table is None else table
    models = table.get("models", {})
    aliases = table.get("aliases", {})
    normalized = str(model or "").strip().lower()
    if not normalized:
        return None

    exact_key = next((key for key in models if str(key).lower() == normalized), None)
    if exact_key is None and isinstance(aliases, dict):
        alias_target = next(
            (value for key, value in aliases.items() if str(key).lower() == normalized),
            None,
        )
        if alias_target in models:
            exact_key = str(alias_target)
    if exact_key is None:
        # Date-stamped provider aliases inherit the base model price only when
        # the alias starts with the complete known model id plus a separator.
        candidates = [str(key) for key in models if normalized.startswith(f"{str(key).lower()}-")]
        exact_key = max(candidates, key=len) if candidates else None
    if exact_key is None:
        return None

    raw = models.get(exact_key)
    if not isinstance(raw, dict):
        return None
    try:
        prices = {field: float(raw.get(field, 0.0) or 0.0) for field in PRICE_FIELDS}
    except (TypeError, ValueError):
        return None
    if any(value < 0 for value in prices.values()):
        return None
    return {
        "model_id": exact_key,
        **prices,
        "updated_at": str(raw.get("updated_at") or table.get("updated_at") or ""),
        "source": str(raw.get("source") or table.get("source") or ""),
    }


def public_pricing_table(table: dict[str, Any] | None = None) -> dict[str, Any]:
    table = load_pricing_table() if table is None else table
    return {
        "default_model": table.get("default_model", ""),
        "updated_at": table.get("updated_at", ""),
        "source": table.get("source", ""),
        "aliases": table.get("aliases", {}),
        "models": table.get("models", {}),
    }
