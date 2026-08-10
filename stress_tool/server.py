"""FastAPI + WebSocket server for the stress test dashboard."""

from __future__ import annotations

import asyncio
import copy
import inspect
import ipaddress
import json
import os
import re
import secrets
import time
from collections import deque
from contextlib import asynccontextmanager, suppress
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit
from uuid import uuid4

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from openai import AsyncOpenAI
from starlette.middleware.trustedhost import TrustedHostMiddleware

from stress_tool.engine_async import BenchmarkEngine
from stress_tool.models import StressConfig
from stress_tool.pricing import load_pricing_table, public_pricing_table, resolve_model_pricing
from stress_tool.prompts import (
    DEFAULT_MODE_KEY,
    get_test_mode,
    public_test_modes,
)
from stress_tool.storage import (
    load_app_state,
    save_app_state,
)

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("SPECTRUMBENCH_DATA_DIR", str(BASE_DIR))).expanduser().resolve()
APP_STATE_FILE = DATA_DIR / "stress_state.json"
LOG_DIR = DATA_DIR / "logs"
RUNTIME_LOG_FILE = LOG_DIR / "token_benchmark_runtime.log"
PRICING_TABLE = load_pricing_table()
STATIC_DIR = Path(__file__).parent / "static"
BROADCAST_QUEUE_MAXSIZE = 1000
WS_SEND_TIMEOUT_SECONDS = 1.0
DROP_HEALTH_BROADCAST_SECONDS = 2.0
STOP_GRACEFUL_TIMEOUT_SECONDS = 5.0
DEFAULT_RETRY_SECONDS = 5
MAX_REPORT_EXPORTS = 4
MAX_HISTORY_ITEMS = 24
MAX_HISTORY_PREVIEWS = 8
MAX_WS_MESSAGE_BYTES = 128 * 1024
APP_STATE_FLUSH_DEBOUNCE_SECONDS = 0.35
RUNTIME_LOG_FLUSH_SECONDS = 0.25
RUNTIME_LOG_BATCH_SIZE = 500
RUNTIME_LOG_MAX_BYTES = 8 * 1024 * 1024
RUNTIME_LOG_ROTATE_KEEP = 2
DROP_WHEN_QUEUE_FULL = {
    "stats",
    "status",
    "log_batch",
    "log",
}
DRAFT_CONFIG_FIELDS = frozenset(
    {
        "model",
        "test_mode",
        "reasoning_effort",
        "max_requests",
        "retry_seconds",
        "run_label",
        "updated_at",
    }
)
HISTORY_CONFIG_FIELDS = frozenset(
    {
        "model",
        "test_mode",
        "mode_version",
        "reasoning_effort",
        "service_tier",
        "max_requests",
        "run_label",
        "num_workers",
        "warmup_rounds",
        "max_output_tokens",
        "pricing_available",
        "pricing_model_id",
        "pricing_updated_at",
    }
)

ALLOWED_HOSTS = [
    host.strip()
    for host in os.getenv(
        "SPECTRUMBENCH_ALLOWED_HOSTS",
        "127.0.0.1,localhost,::1,testserver",
    ).split(",")
    if host.strip()
]
_CONFIGURED_SESSION_TOKEN = os.getenv("SPECTRUMBENCH_SESSION_TOKEN", "").strip()
if _CONFIGURED_SESSION_TOKEN and len(_CONFIGURED_SESSION_TOKEN) < 32:
    raise ValueError("SPECTRUMBENCH_SESSION_TOKEN must contain at least 32 characters")
SESSION_TOKEN = _CONFIGURED_SESSION_TOKEN or secrets.token_urlsafe(32)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    await startup_event()
    try:
        yield
    finally:
        await shutdown_event()


app = FastAPI(
    title="SpectrumBench",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,
)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=ALLOWED_HOSTS)

# Global state
engine: BenchmarkEngine | None = None
connected_clients: set[WebSocket] = set()
_engine_task: asyncio.Task | None = None
_broadcast_task: asyncio.Task | None = None
_broadcast_queue: asyncio.Queue[str] = asyncio.Queue(maxsize=BROADCAST_QUEUE_MAXSIZE)
_last_run_result: dict[str, Any] | None = None
_report_exports: dict[str, dict[str, str]] = {}
_broadcast_drop_stats: dict[str, int] = {}
_last_broadcast_health_at = 0.0
_app_state_cache: dict[str, Any] | None = None
_app_state_flush_task: asyncio.Task | None = None
_app_state_dirty = False
_app_state_version = 0
_app_state_last_mutation_at = 0.0
_runtime_log_buffer: deque[str] = deque()
_runtime_log_task: asyncio.Task | None = None
_runtime_log_lock: asyncio.Lock | None = None


async def startup_event() -> None:
    global _broadcast_task, _runtime_log_lock
    clean_state = _load_app_state_safe(force_reload=True)
    # Upgrade old installations immediately: credentials and endpoints are never
    # allowed to survive in the persisted draft.
    await asyncio.to_thread(save_app_state, APP_STATE_FILE, copy.deepcopy(clean_state))
    _runtime_log_lock = asyncio.Lock()
    if _broadcast_task is None or _broadcast_task.done():
        _broadcast_task = asyncio.create_task(_broadcast_loop())


async def shutdown_event() -> None:
    global _broadcast_task
    await _flush_app_state_now()
    await _flush_runtime_log_now()
    if _broadcast_task and not _broadcast_task.done():
        _broadcast_task.cancel()
        with suppress(asyncio.CancelledError):
            await _broadcast_task
    _broadcast_task = None


# ── HTML serving ─────────────────────────────────────────────


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    if "Content-Security-Policy" not in response.headers:
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; base-uri 'none'; frame-ancestors 'none'; "
            "form-action 'self'; img-src 'self' data:; font-src 'self'; "
            "style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'"
        )
    response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
    response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=(), payment=()"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    return response


@app.get("/", response_class=HTMLResponse)
async def index():
    html_path = STATIC_DIR / "index.html"
    nonce = secrets.token_urlsafe(24)
    html = html_path.read_text("utf-8").replace("<script>", f'<script nonce="{nonce}">')
    response = HTMLResponse(content=html)
    response.headers["Content-Security-Policy"] = (
        "default-src 'none'; base-uri 'none'; frame-ancestors 'none'; "
        "form-action 'self'; img-src 'self' data:; font-src 'self'; "
        "style-src 'self' 'unsafe-inline'; "
        f"script-src 'nonce-{nonce}'; connect-src 'self'"
    )
    return response


@app.get("/favicon.ico")
async def favicon():
    return FileResponse(STATIC_DIR / "favicon.ico", media_type="image/x-icon")


@app.get("/favicon.svg")
async def favicon_svg():
    return FileResponse(STATIC_DIR / "favicon.svg", media_type="image/svg+xml")


@app.get("/favicon-32.png")
async def favicon_png():
    return FileResponse(STATIC_DIR / "favicon-32.png", media_type="image/png")


@app.get("/healthz")
async def healthz():
    return JSONResponse({"app": "SpectrumBench", "status": "ok", "version": 1})


@app.get("/download-report/{report_id}")
async def download_report(report_id: str):
    export = _report_exports.pop(report_id, None)
    if not export:
        return Response(content="Report not found", status_code=404, media_type="text/plain")
    filename = export["filename"]
    quoted_name = quote(filename)
    return Response(
        content=export["content"],
        media_type="application/json",
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quoted_name}",
            "Cache-Control": "no-store",
        },
    )


# ── WebSocket endpoint ───────────────────────────────────────


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    if not _websocket_origin_allowed(ws) or not _websocket_session_allowed(ws):
        await ws.close(code=1008, reason="WebSocket access denied")
        return
    await ws.accept(subprotocol="spectrumbench")
    connected_clients.add(ws)
    # Send initial state
    try:
        app_state = _load_app_state_safe()
        history_entries = _history_entries_from_state(app_state)
        await ws.send_text(
            json.dumps(
                {
                    "type": "init",
                    "data": {
                        "startup_draft": _resolve_startup_draft(app_state),
                        "test_modes": public_test_modes(),
                        "default_mode": DEFAULT_MODE_KEY,
                        "pricing_table": public_pricing_table(PRICING_TABLE),
                        "running": engine.is_running if engine else False,
                        "detecting": False,
                        "has_result": _has_exportable_result(
                            _capture_engine_result(engine, engine.config) if engine else _last_run_result
                        ),
                        "history": history_entries,
                        "runtime_log_path": "logs/token_benchmark_runtime.log",
                    },
                },
                ensure_ascii=False,
            )
        )
        if (engine and engine.is_running) or (_engine_task is not None and not _engine_task.done()):
            await _send_to(ws, "stats", engine.get_stats_snapshot())
            await _send_to(ws, "status", _current_status_payload(engine))
        broadcast_health = _broadcast_health_payload()
        if broadcast_health is not None:
            await _send_to(ws, "broadcast_health", broadcast_health)
    except Exception:
        pass

    try:
        while True:
            raw = await ws.receive_text()
            if len(raw.encode("utf-8")) > MAX_WS_MESSAGE_BYTES:
                await ws.close(code=1009, reason="Message too large")
                break
            msg = json.loads(raw)
            if not isinstance(msg, dict):
                await _send_to(ws, "error", {"message": "消息格式错误"})
                continue
            await _handle_message(ws, msg)
    except WebSocketDisconnect:
        connected_clients.discard(ws)
    except Exception:
        connected_clients.discard(ws)


async def _handle_message(ws: WebSocket, msg: dict) -> None:
    global engine, _engine_task, _last_run_result
    msg_type = msg.get("type", "")

    if msg_type == "start":
        if (engine and engine.is_running) or (_engine_task is not None and not _engine_task.done()):
            await _send_to(
                ws,
                "error",
                _error_payload(
                    "压测已在运行中",
                    "start",
                    running=True,
                    detecting=False,
                ),
            )
            return
        try:
            config = _build_config(msg.get("config", {}))
        except (ValueError, KeyError) as e:
            await _send_to(
                ws,
                "error",
                _error_payload(str(e), "start", running=False, detecting=False),
            )
            return

        _last_run_result = None
        _reset_broadcast_drop_stats()
        engine = BenchmarkEngine()

        async def emit(event_type: str, data: Any) -> None:
            _queue_broadcast(event_type, data)

        async def run_engine(current_engine: BenchmarkEngine, current_config: StressConfig) -> None:
            global _engine_task, _last_run_result
            try:
                await _send_to(
                    ws, "log", {"message": "使用 Responses API 流式模式；不会回退到 Chat Completions 兼容模式。"}
                )
                await current_engine.start(current_config, emit)
            finally:
                _last_run_result = _capture_engine_result(current_engine, current_config)
                if _last_run_result is not None:
                    _queue_broadcast(
                        "result_ready",
                        {
                            "stats": _last_run_result.get("stats", {}),
                            "config": _last_run_result.get("config", {}),
                            "rounds": (_last_run_result.get("rounds") or [])[-200:],
                        },
                    )
                history_entries = _remember_run_history(_last_run_result, current_config)
                if history_entries is not None:
                    _schedule_app_state_flush()
                    _queue_broadcast("history_updated", {"entries": history_entries})
                _engine_task = None

        _engine_task = asyncio.create_task(run_engine(engine, config))

    elif msg_type == "stop":
        if engine:
            engine.request_stop()
            _queue_broadcast("status", {"text": "正在结束...", "color": "#f59e0b"})
            _queue_broadcast(
                "log", {"message": "用户请求停止；未完成请求的部分内容只作估算，不会混入精确 usage 总额。"}
            )
            task = _engine_task
            if task and not task.done():
                done, pending = await asyncio.wait({task}, timeout=STOP_GRACEFUL_TIMEOUT_SECONDS)
                if pending:
                    _queue_broadcast(
                        "log", {"message": "结束等待超时，已强制释放后台任务；未完成请求会单列为中断估算。"}
                    )
                    engine.stop()
                    task.cancel()
                    await asyncio.wait({task}, timeout=2.0)
                if done or task.done():
                    with suppress(asyncio.CancelledError, Exception):
                        await task
            _last_run_result = _capture_engine_result(engine, engine.config)
            engine = None
            _engine_task = None

    elif msg_type == "query_models":
        cfg = msg.get("config", {})
        api_key = cfg.get("api_key", "").strip()
        base_url = cfg.get("base_url", "").strip()
        if not api_key or not base_url:
            await _send_to(ws, "error", {"message": "请先填写 API Key 和 Base URL"})
            return
        asyncio.create_task(_query_models(ws, api_key, base_url))

    elif msg_type == "save_draft":
        data = msg.get("data", {})
        if not isinstance(data, dict):
            return
        try:
            _remember_app_state(draft=data)
            _schedule_app_state_flush()
        except Exception as e:
            await _send_to(ws, "error", {"message": f"本地记忆保存失败: {_safe_error_text(e)}"})

    elif msg_type == "export_report":
        current_result = _capture_engine_result(engine, engine.config) if engine else None
        result = current_result if engine is not None else _last_run_result
        if not _has_exportable_result(result):
            await _send_to(ws, "error", {"message": "暂无可导出的运行结果"})
            return
        snap = result["stats"]
        records = result["rounds"]
        public_config = result.get("config") or {}
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        report = {
            "exported_at": datetime.now().isoformat(timespec="seconds"),
            "config": public_config,
            "stats": snap,
            "meta": result.get("meta", {}),
            "rounds": records,
        }
        report_id = uuid4().hex
        _report_exports[report_id] = {
            "filename": f"stress_report_{stamp}.json",
            "content": json.dumps(report, ensure_ascii=False, indent=2),
        }
        while len(_report_exports) > MAX_REPORT_EXPORTS:
            oldest_id = next(iter(_report_exports))
            _report_exports.pop(oldest_id, None)
        await _send_to(
            ws,
            "report_ready",
            {
                "filename": f"stress_report_{stamp}.json",
                "report_id": report_id,
            },
        )

    elif msg_type == "clear_runtime_state":
        if engine and engine.is_running:
            await _send_to(
                ws,
                "error",
                _error_payload("运行中不能清空统计，请先停止压测", "clear_runtime_state"),
            )
            return
        _last_run_result = None
        _report_exports.clear()
        _reset_broadcast_drop_stats()
        engine = None
        _engine_task = None
        _queue_broadcast("runtime_cleared", {"message": "统计已清空"})


async def _query_models(ws: WebSocket, api_key: str, base_url: str) -> None:
    await _send_to(ws, "log", {"message": "正在查询模型列表..."})
    client: AsyncOpenAI | None = None
    try:
        normalized_url = _normalize_base_url(base_url)
        client = AsyncOpenAI(api_key=api_key, base_url=normalized_url, timeout=15.0)
        models_resp = await client.models.list()
        model_ids = sorted(m.id for m in models_resp.data)
        await _send_to(ws, "models", {"ids": model_ids})
        await _send_to(ws, "log", {"message": f"查询完成，共 {len(model_ids)} 个模型。"})
    except Exception as e:
        await _send_to(
            ws,
            "error",
            {"message": f"查询模型失败: {_safe_error_text(e, api_key, base_url)}"},
        )
    finally:
        if client is not None:
            await _close_async_client(client)


def _build_config(raw: dict) -> StressConfig:
    if not isinstance(raw, dict):
        raise ValueError("配置格式错误")
    api_key = raw.get("api_key", "").strip()
    base_url = _normalize_base_url(raw.get("base_url", "").strip())
    model = raw.get("model", "").strip()
    if not api_key:
        raise ValueError("请填写 API Key")
    if not base_url:
        raise ValueError("请填写 Base URL")
    if not model:
        raise ValueError("请选择或输入模型名称")

    mode = get_test_mode(str(raw.get("test_mode") or DEFAULT_MODE_KEY))
    reasoning_effort = str(raw.get("reasoning_effort") or mode.default_reasoning).strip().lower()
    if reasoning_effort not in mode.allowed_reasoning:
        allowed = "、".join(mode.allowed_reasoning)
        raise ValueError(f"{mode.name} 只允许推理等级: {allowed}")
    max_requests = _read_int_field(
        raw.get("max_requests"),
        "最大请求数",
        default=mode.default_max_requests,
        minimum=0,
        maximum=100000,
    )
    pricing = resolve_model_pricing(model, PRICING_TABLE)
    run_label = str(raw.get("run_label") or "").strip()[:80]

    return StressConfig(
        api_key=api_key,
        base_url=base_url,
        model=model,
        test_mode=mode.key,
        reasoning_effort=reasoning_effort,
        service_tier="default",
        system_prompt=mode.system_prompt,
        continue_prompt=mode.turn_prompts[0],
        max_rounds=(max_requests + mode.warmup_rounds) if max_requests > 0 else 0,
        request_interval_seconds=mode.request_interval_seconds,
        retry_seconds=_read_int_field(
            raw.get("retry_seconds"),
            "重试等待",
            default=DEFAULT_RETRY_SECONDS,
            minimum=0,
        ),
        run_label=run_label,
        num_workers=1,
        max_output_tokens=mode.max_output_tokens,
        warmup_rounds=mode.warmup_rounds,
        input_price_per_million=float(pricing["input"]) if pricing else 0.0,
        output_price_per_million=float(pricing["output"]) if pricing else 0.0,
        cached_price_per_million=float(pricing["cached_input"]) if pricing else 0.0,
        cache_write_price_per_million=float(pricing["cache_write"]) if pricing else 0.0,
        pricing_model_id=str(pricing["model_id"]) if pricing else "",
        pricing_updated_at=str(pricing["updated_at"]) if pricing else "",
        pricing_source=str(pricing["source"]) if pricing else "",
        context_keep_recent=16,
    )


def _normalize_base_url(base_url: str) -> str:
    value = (base_url or "").strip()
    if not value:
        return value
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError as error:
        raise ValueError("Base URL 格式无效") from error
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise ValueError("Base URL 必须是完整的 http:// 或 https:// 地址")
    if parts.username is not None or parts.password is not None:
        raise ValueError("Base URL 不能包含用户名或密码")
    if parts.query or parts.fragment:
        raise ValueError("Base URL 不能包含查询参数或片段")
    hostname = (parts.hostname or "").strip().lower()
    if not hostname:
        raise ValueError("Base URL 缺少主机名")
    if parts.scheme == "http" and not _is_loopback_host(hostname):
        raise ValueError("远程 Base URL 必须使用 HTTPS；HTTP 仅允许 localhost 或回环地址")
    path = parts.path.rstrip("/")
    if path.endswith("/v1"):
        return value.rstrip("/")
    if path in ("", "/"):
        path = "/v1"
    netloc = parts.netloc.lower()
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("Base URL 端口必须在 1–65535 之间")
    normalized = urlunsplit((parts.scheme, netloc, path, "", ""))
    return normalized.rstrip("/")


def _is_loopback_host(hostname: str) -> bool:
    if hostname in {"localhost", "localhost.localdomain"}:
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def _websocket_origin_allowed(ws: WebSocket) -> bool:
    origin = (ws.headers.get("origin") or "").strip()
    if not origin:
        # Browsers always send Origin. Keeping non-browser clients compatible is
        # safe because cross-site browser attacks cannot omit this header.
        return True
    try:
        parsed = urlsplit(origin)
    except ValueError:
        return False
    host = (ws.headers.get("host") or "").strip().lower()
    return parsed.scheme in {"http", "https"} and parsed.netloc.lower() == host


def _websocket_session_allowed(ws: WebSocket) -> bool:
    offered = {item.strip() for item in (ws.headers.get("sec-websocket-protocol") or "").split(",") if item.strip()}
    supplied = next((item[8:] for item in offered if item.startswith("session.")), "")
    return "spectrumbench" in offered and secrets.compare_digest(supplied, SESSION_TOKEN)


def _read_int_field(
    value: Any,
    field_name: str,
    default: int,
    minimum: int,
    maximum: int | None = None,
) -> int:
    raw = "" if value is None else str(value).strip()
    if raw == "":
        parsed = default
    else:
        try:
            parsed = int(raw)
        except ValueError as error:
            raise ValueError(f"{field_name}必须是整数") from error
    if parsed < minimum:
        raise ValueError(f"{field_name}不能小于 {minimum}")
    if maximum is not None and parsed > maximum:
        raise ValueError(f"{field_name}不能大于 {maximum}")
    return parsed


def _error_payload(message: str, action: str | None = None, **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"message": message}
    if action:
        payload["action"] = action
    payload.update(extra)
    return payload


def _safe_error_text(error: Exception, *sensitive_values: str) -> str:
    text = BenchmarkEngine._exception_text(error, sensitive_values=sensitive_values).strip()
    return (text or error.__class__.__name__)[:600]


def _current_status_payload(current_engine: BenchmarkEngine | None) -> dict[str, str]:
    if current_engine is None or not current_engine.is_running:
        return {"text": "未运行", "color": ""}
    snap = current_engine.get_stats_snapshot()
    return {
        "text": f"运行中 · {snap.get('request_count', snap['round_count'])} 请求",
        "color": "#22c55e",
    }


async def _close_async_client(client: AsyncOpenAI) -> None:
    close = getattr(client, "close", None) or getattr(client, "aclose", None)
    if close is None:
        return
    try:
        result = close()
        if inspect.isawaitable(result):
            await result
    except Exception:
        pass


# ── Broadcasting ─────────────────────────────────────────────


def _sanitize_frontend_config_payload(data: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(data, dict):
        return {}
    return {key: value for key, value in data.items() if key in DRAFT_CONFIG_FIELDS}


def _sanitize_history_entry(entry: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(entry, dict):
        return None
    cleaned = dict(entry)
    raw_config = cleaned.get("config")
    cleaned["config"] = (
        {key: value for key, value in raw_config.items() if key in HISTORY_CONFIG_FIELDS}
        if isinstance(raw_config, dict)
        else {}
    )
    raw_previews = cleaned.get("round_previews")
    if isinstance(raw_previews, list):
        cleaned["round_previews"] = [
            {key: value for key, value in item.items() if key != "preview"}
            for item in raw_previews
            if isinstance(item, dict)
        ]
    return cleaned


def _load_app_state_safe(force_reload: bool = False) -> dict[str, Any]:
    global _app_state_cache
    if force_reload or _app_state_cache is None:
        try:
            state = load_app_state(APP_STATE_FILE)
        except Exception:
            state = {}
        source_state = state if isinstance(state, dict) else {}
        # Rebuild from an allowlist so legacy profile, credential, endpoint or
        # unknown fields cannot survive an upgrade.
        clean_state = {
            "draft": _sanitize_frontend_config_payload(source_state.get("draft")),
            "run_history": _history_entries_from_state(source_state),
        }
        _app_state_cache = clean_state
    return _app_state_cache


def _schedule_app_state_flush() -> None:
    global _app_state_flush_task
    if _app_state_flush_task is None or _app_state_flush_task.done():
        _app_state_flush_task = asyncio.create_task(_flush_app_state_loop())


async def _flush_app_state_loop() -> None:
    global _app_state_dirty, _app_state_flush_task
    try:
        while _app_state_dirty:
            remaining = APP_STATE_FLUSH_DEBOUNCE_SECONDS - (time.monotonic() - _app_state_last_mutation_at)
            if remaining > 0:
                await asyncio.sleep(remaining)
            version = _app_state_version
            state_copy = copy.deepcopy(_load_app_state_safe())
            await asyncio.to_thread(save_app_state, APP_STATE_FILE, state_copy)
            if version == _app_state_version:
                _app_state_dirty = False
    finally:
        _app_state_flush_task = None


async def _flush_app_state_now() -> None:
    global _app_state_flush_task, _app_state_last_mutation_at
    if _app_state_cache is None:
        return
    if not _app_state_dirty and (_app_state_flush_task is None or _app_state_flush_task.done()):
        return
    _app_state_last_mutation_at = float("-inf")
    if _app_state_flush_task is not None and not _app_state_flush_task.done():
        _app_state_flush_task.cancel()
        with suppress(asyncio.CancelledError):
            await _app_state_flush_task
    _schedule_app_state_flush()
    if _app_state_flush_task is not None:
        await _app_state_flush_task


def _remember_app_state(draft: dict[str, Any] | None = None) -> None:
    global _app_state_dirty, _app_state_last_mutation_at, _app_state_version
    state = _load_app_state_safe()
    if draft is not None:
        state["draft"] = _sanitize_frontend_config_payload(draft)
    _app_state_dirty = True
    _app_state_version += 1
    _app_state_last_mutation_at = time.monotonic()


def _remember_run_history(
    result: dict[str, Any] | None,
    current_config: StressConfig | None,
) -> list[dict[str, Any]] | None:
    entry = _build_history_entry(result, current_config)
    if entry is None:
        return None
    global _app_state_dirty, _app_state_last_mutation_at, _app_state_version
    state = _load_app_state_safe()
    history = [entry, *_history_entries_from_state(state)]
    state["run_history"] = history[:MAX_HISTORY_ITEMS]
    _app_state_dirty = True
    _app_state_version += 1
    _app_state_last_mutation_at = time.monotonic()
    return list(state["run_history"])


def _resolve_startup_draft(app_state: dict[str, Any]) -> dict[str, Any] | None:
    draft = app_state.get("draft")
    if not isinstance(draft, dict):
        return None
    return _sanitize_frontend_config_payload(draft)


def _capture_engine_result(
    current_engine: BenchmarkEngine | None, current_config: StressConfig | None
) -> dict[str, Any] | None:
    if current_engine is None:
        return None
    return {
        "stats": current_engine.get_stats_snapshot(),
        "rounds": [
            {key: value for key, value in record.items() if key != "preview"} for record in current_engine.get_records()
        ],
        "meta": current_engine.get_report_meta(),
        "config": _config_to_frontend_payload(current_config) if current_config else None,
    }


def _has_exportable_result(result: dict[str, Any] | None) -> bool:
    if not isinstance(result, dict):
        return False
    stats = result.get("stats")
    if not isinstance(stats, dict):
        return False
    try:
        round_count = int(stats.get("round_count", 0) or 0)
        request_count = int(stats.get("request_count", 0) or 0)
        successful_requests = int(stats.get("successful_requests", 0) or 0)
        failed_requests = int(stats.get("failed_requests", 0) or 0)
        total_tokens = int(stats.get("total_tokens", 0) or 0)
    except (TypeError, ValueError):
        return False
    rounds = result.get("rounds")
    return (
        round_count > 0
        or request_count > 0
        or successful_requests > 0
        or failed_requests > 0
        or total_tokens > 0
        or (isinstance(rounds, list) and len(rounds) > 0)
    )


def _history_entries_from_state(app_state: dict[str, Any]) -> list[dict[str, Any]]:
    raw = app_state.get("run_history")
    if not isinstance(raw, list):
        return []
    entries: list[dict[str, Any]] = []
    for item in raw:
        cleaned = _sanitize_history_entry(item)
        if cleaned is not None:
            entries.append(cleaned)
        if len(entries) >= MAX_HISTORY_ITEMS:
            break
    return entries


def _build_history_entry(
    result: dict[str, Any] | None,
    current_config: StressConfig | None,
) -> dict[str, Any] | None:
    if current_config is None or not _has_exportable_result(result):
        return None
    assert result is not None
    rounds = result.get("rounds")
    previews: list[dict[str, Any]] = []
    if isinstance(rounds, list):
        for item in rounds[-MAX_HISTORY_PREVIEWS:]:
            if not isinstance(item, dict):
                continue
            previews.append(
                {
                    "round_index": item.get("round_index"),
                    "worker_id": item.get("worker_id"),
                    "total_tokens": item.get("total_tokens", 0),
                    "visible_tokens_per_second": item.get("visible_tokens_per_second", 0),
                    "end_to_end_visible_tokens_per_second": item.get("end_to_end_visible_tokens_per_second", 0),
                    "first_token_latency_ms": item.get("first_token_latency_ms", 0),
                    "cached_tokens": item.get("cached_tokens", 0),
                    "is_warmup": item.get("is_warmup", False),
                    "finish_reason": item.get("finish_reason", ""),
                }
            )
    return {
        "id": uuid4().hex,
        "finished_at": datetime.now().isoformat(timespec="seconds"),
        "stats": result.get("stats", {}),
        "meta": result.get("meta", {}),
        "config": _config_to_frontend_payload(current_config),
        "round_previews": previews,
    }


def _config_to_frontend_payload(config: StressConfig) -> dict[str, Any]:
    mode = get_test_mode(config.test_mode)
    return {
        "model": config.model,
        "test_mode": config.test_mode,
        "mode_version": mode.version,
        "reasoning_effort": config.reasoning_effort,
        "service_tier": config.service_tier,
        "max_requests": str(config.formal_max_rounds),
        "interval_seconds": _stringify_number(config.request_interval_seconds),
        "retry_seconds": str(config.retry_seconds),
        "run_label": config.run_label,
        "num_workers": "1",
        "warmup_rounds": config.warmup_rounds,
        "max_output_tokens": config.max_output_tokens,
        "pricing_available": config.pricing_available,
        "pricing_model_id": config.pricing_model_id,
        "pricing_updated_at": config.pricing_updated_at,
        "pricing_source": config.pricing_source,
        "input_price_per_million": config.input_price_per_million,
        "output_price_per_million": config.output_price_per_million,
        "cached_price_per_million": config.cached_price_per_million,
        "cache_write_price_per_million": config.cache_write_price_per_million,
    }


def _stringify_number(value: float) -> str:
    if float(value).is_integer():
        return str(int(value))
    return str(value)


def _append_runtime_log_lines(lines: list[str]) -> None:
    if not lines:
        return
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    _rotate_runtime_log_if_needed(sum(len(line.encode("utf-8")) for line in lines))
    with RUNTIME_LOG_FILE.open("a", encoding="utf-8", newline="\n") as handle:
        handle.writelines(lines)


def _rotate_runtime_log_if_needed(incoming_bytes: int = 0) -> None:
    if not RUNTIME_LOG_FILE.exists():
        return
    try:
        if RUNTIME_LOG_FILE.stat().st_size + max(int(incoming_bytes), 0) <= RUNTIME_LOG_MAX_BYTES:
            return
        oldest = LOG_DIR / f"{RUNTIME_LOG_FILE.name}.{RUNTIME_LOG_ROTATE_KEEP}"
        if oldest.exists():
            oldest.unlink()
        for index in range(RUNTIME_LOG_ROTATE_KEEP - 1, 0, -1):
            src = LOG_DIR / f"{RUNTIME_LOG_FILE.name}.{index}"
            if src.exists():
                src.replace(LOG_DIR / f"{RUNTIME_LOG_FILE.name}.{index + 1}")
        RUNTIME_LOG_FILE.replace(LOG_DIR / f"{RUNTIME_LOG_FILE.name}.1")
    except OSError:
        pass


def _queue_runtime_log_lines(lines: list[str]) -> None:
    if not lines:
        return
    _runtime_log_buffer.extend(lines)
    _schedule_runtime_log_flush()


def _schedule_runtime_log_flush() -> None:
    global _runtime_log_task
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        _flush_runtime_log_sync()
        return
    if _runtime_log_task is None or _runtime_log_task.done():
        _runtime_log_task = loop.create_task(_flush_runtime_log_loop())


async def _flush_runtime_log_loop() -> None:
    global _runtime_log_task
    try:
        while _runtime_log_buffer:
            await asyncio.sleep(RUNTIME_LOG_FLUSH_SECONDS)
            await _flush_runtime_log_now()
    finally:
        _runtime_log_task = None


async def _flush_runtime_log_now() -> None:
    if not _runtime_log_buffer:
        return
    lock = _runtime_log_lock or asyncio.Lock()
    async with lock:
        while _runtime_log_buffer:
            batch: list[str] = []
            while _runtime_log_buffer and len(batch) < RUNTIME_LOG_BATCH_SIZE:
                batch.append(_runtime_log_buffer.popleft())
            await asyncio.to_thread(_append_runtime_log_lines, batch)


def _flush_runtime_log_sync() -> None:
    if not _runtime_log_buffer:
        return
    batch = list(_runtime_log_buffer)
    _runtime_log_buffer.clear()
    with suppress(Exception):
        _append_runtime_log_lines(batch)


def _message_from_log_payload(payload: Any) -> str:
    if isinstance(payload, dict):
        message = payload.get("message")
        if isinstance(message, str):
            return message
    if isinstance(payload, str):
        return payload
    return json.dumps(payload, ensure_ascii=False)


def _runtime_log_entries(event_type: str, data: Any) -> list[tuple[str, str]]:
    if event_type == "log":
        return [("INFO", _message_from_log_payload(data))]
    if event_type == "log_batch" and isinstance(data, list):
        return [("INFO", _message_from_log_payload(item)) for item in data]
    if event_type == "error":
        return [("ERROR", _message_from_log_payload(data))]
    if event_type == "status" and isinstance(data, dict):
        status_text = str(data.get("text", "")).strip()
        if status_text:
            return [("STATUS", status_text)]
    if event_type == "finished" and isinstance(data, dict):
        return [
            (
                "INFO",
                "压测完成 | "
                f"请求 {data.get('request_count', data.get('round_count', 0))} | "
                f"总 Token {data.get('total_tokens', 0)} | "
                f"成功 {data.get('successful_requests', data.get('successful_rounds', 0))} | "
                f"失败 {data.get('failed_requests', data.get('failed_rounds', 0))}",
            )
        ]
    if event_type == "report_ready" and isinstance(data, dict):
        filename = str(data.get("filename", "")).strip()
        if filename:
            return [("INFO", f"报告已生成: {filename}")]
    return []


def _mirror_event_to_runtime_log(event_type: str, data: Any) -> None:
    entries = _runtime_log_entries(event_type, data)
    if not entries:
        return
    stamp = datetime.now().isoformat(timespec="seconds")
    lines = []
    for level, message in entries:
        if not message:
            continue
        clean_message = re.sub(r"[\x00-\x1f\x7f-\x9f]+", " ", str(message))[:800]
        lines.append(f"[{stamp}] [{level}] {clean_message}\n")
    if not lines:
        return
    with suppress(Exception):
        _queue_runtime_log_lines(lines)


def _broadcast_health_payload() -> dict[str, Any] | None:
    if not _broadcast_drop_stats:
        return None
    dropped_by_type = {key: int(value) for key, value in sorted(_broadcast_drop_stats.items()) if int(value) > 0}
    if not dropped_by_type:
        return None
    dropped_total = sum(dropped_by_type.values())
    return {
        "degraded": dropped_total > 0,
        "dropped_total": dropped_total,
        "dropped_by_type": dropped_by_type,
        "message": f"前端更新已发生丢失，当前共丢弃 {dropped_total} 条低优先级更新。",
    }


def _reset_broadcast_drop_stats() -> None:
    _broadcast_drop_stats.clear()


def _record_broadcast_drop(event_type: str) -> None:
    global _last_broadcast_health_at
    _broadcast_drop_stats[event_type] = _broadcast_drop_stats.get(event_type, 0) + 1
    now = time.time()
    if now - _last_broadcast_health_at < DROP_HEALTH_BROADCAST_SECONDS:
        return
    payload = _broadcast_health_payload()
    if payload is None:
        return
    _last_broadcast_health_at = now
    asyncio.create_task(
        _broadcast_payload(json.dumps({"type": "broadcast_health", "data": payload}, ensure_ascii=False))
    )


def _queue_broadcast(event_type: str, data: Any) -> None:
    _mirror_event_to_runtime_log(event_type, data)
    payload = json.dumps({"type": event_type, "data": data}, ensure_ascii=False)
    try:
        _broadcast_queue.put_nowait(payload)
    except asyncio.QueueFull:
        if event_type in DROP_WHEN_QUEUE_FULL:
            _record_broadcast_drop(event_type)
            return
        asyncio.create_task(_broadcast_payload(payload))


async def _broadcast_loop() -> None:
    while True:
        payload = await _broadcast_queue.get()
        try:
            await _broadcast_payload(payload)
        finally:
            _broadcast_queue.task_done()


async def _broadcast_payload(payload: str) -> None:
    if not connected_clients:
        return

    async def _safe_send(ws: WebSocket) -> WebSocket | None:
        try:
            await asyncio.wait_for(ws.send_text(payload), timeout=WS_SEND_TIMEOUT_SECONDS)
            return None
        except Exception:
            return ws

    results = await asyncio.gather(*[_safe_send(ws) for ws in connected_clients])
    for ws in results:
        if ws is not None:
            connected_clients.discard(ws)


async def _send_to(ws: WebSocket, event_type: str, data: Any) -> None:
    _mirror_event_to_runtime_log(event_type, data)
    try:
        await asyncio.wait_for(
            ws.send_text(json.dumps({"type": event_type, "data": data}, ensure_ascii=False)),
            timeout=WS_SEND_TIMEOUT_SECONDS,
        )
    except Exception:
        connected_clients.discard(ws)
