"""Atomic persistence helpers for local app state."""

from __future__ import annotations

import json
import os
import tempfile
from contextlib import suppress
from pathlib import Path
from typing import Any


def _load_json_object(file_path: Path, error_message: str) -> dict[str, Any]:
    if not file_path.exists():
        return {}
    content = file_path.read_text(encoding="utf-8").strip()
    if not content:
        return {}
    try:
        raw = json.loads(content)
    except json.JSONDecodeError as error:
        raise ValueError(f"{error_message}（JSON 解析失败: {error.msg}）") from error
    if not isinstance(raw, dict):
        raise ValueError(error_message)
    return raw


def _save_json_object(file_path: Path, payload: dict[str, Any]) -> None:
    file_path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(payload, ensure_ascii=False, indent=2)
    _atomic_write_text(file_path, content)


def _atomic_write_text(file_path: Path, content: str) -> None:
    file_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=file_path.parent,
            prefix=f".{file_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
            temp_path = Path(handle.name)
        os.replace(temp_path, file_path)
    finally:
        if temp_path is not None and temp_path.exists():
            with suppress(OSError):
                temp_path.unlink()


def load_app_state(file_path: Path) -> dict[str, Any]:
    return _load_json_object(file_path, "状态文件格式错误，必须是对象结构")


def save_app_state(file_path: Path, state: dict[str, Any]) -> None:
    _save_json_object(file_path, state)
